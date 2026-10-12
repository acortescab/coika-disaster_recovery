# coika-disaster_recovery

Code of the lambdas used by the AWS disaster recovery of the Coika services. Each lambda is packaged as a container image, validated in CI (ruff + pytest) and pushed to ECR when merged to `main`.

The ECR repository of each lambda is defined in `infra/` in this repo. The rest of the infrastructure (the functions with their execution roles, environment and permissions, alarms, Step Functions, CodeBuild, GitHub OIDC roles) lives in the sibling repository **coika-disaster_recovery-infra**, which looks the repositories up.

## Structure

```
lambdas/<service>/<lambda>/    # one folder per lambda (app.py, Dockerfile, requirements*.txt, tests/)
.github/
  workflows/ci.yml             # on PR: detects changed lambdas and runs lambda-ci.yml in a matrix
  workflows/lambda-ci.yml      # reusable: lint, test and build (no push) for ONE lambda
  workflows/cd.yml             # on push to main: detects changed lambdas and runs lambda-cd.yml in a matrix
  workflows/lambda-cd.yml      # reusable: build, push to ECR and update the function for ONE lambda
  scripts/changed_lambdas.py   # computes the matrix of affected lambdas
  workflows/terraform-ci.yml   # on PR: fmt, validate and plan of infra/ (the plan is commented on the PR)
  workflows/terraform-cd.yml   # on push to main: plan + apply of infra/ (needs approval of the `prod` environment)
infra/
  main.tf                      # one module "ecr_repository" call per lambda
  modules/ecr_repository/      # ECR repository + lifecycle policy
ruff.toml                      # shared lint config
```

Current lambdas:

| Lambda | Description |
| --- | --- |
| `auth_service/health_check` | Sends a GET to `HEALTH_URL` and publishes the `BackendHealth` metric to CloudWatch (`Coika/DisasterRecovery`). |
| `auth_service/restore_db` | Called by the failover state machine after the deploy: runs `restore.sh --yes --if-empty` on the DB host of the DR region through SSM and waits for it. It returns `InProgress` if the restore outlasts the invocation, and the state machine calls it again. |
| `auth_service/primary_recovery_check` | Runs on a schedule in the DR region. While the failover state is `DR_ACTIVE` it probes the primary region (every probe must answer, plus the optional `PRIMARY_HEALTH_URL`) and counts consecutive healthy runs in `recovery_streak`; any failure resets it. At `RECOVERY_THRESHOLD` it sends one SNS notification that a failback is possible. In any other state it does nothing. |

## Adding a new lambda

1. Create `lambdas/<service>/<name>/` with `app.py`, `Dockerfile`, `requirements.txt`, `requirements-dev.txt` and `tests/` (copy an existing one).
2. In `infra/main.tf`, add a `module "ecr_repository"` call for it (it creates the ECR repository `<ECR_REPOSITORY_PREFIX>/<service>/<name>`) and apply it.
3. Merge the code to `main`: the lambda CD builds and pushes the first image. Its `update-function-code` step is skipped with a warning while the function does not exist yet.
4. In the infra repository, add the `module "lambda_container"` call (function `<LAMBDA_NAME_PREFIX>-<service>-<name>`, role, environment, permissions) and apply it. The function is created from the image pushed in step 3.

No YAML needs to be touched: `ci.yml` and `cd.yml` discover any `lambdas/*/*/` folder that has a `Dockerfile`. The repository and function names are computed in `changed_lambdas.py` and must stay in sync with `infra/modules/ecr_repository` and the `lambda_container` module of the infra repository.

## Local development

```powershell
cd lambdas/<service>/<name>
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
ruff check .
pytest
```

## CI / CD

- Only lambdas with changes are processed. If something shared changes (`ruff.toml`, the workflows or `.github/scripts/`), all of them are processed.
- **CI** (`ci.yml`, on pull requests): lint + tests + image build. Nothing is pushed and no AWS access is needed.
- **CD** (`cd.yml`, on push to `main`): assumes the AWS role via OIDC, pushes the image to ECR with `<sha>` and `latest` tags and runs `aws lambda update-function-code`. Deployments are never cancelled by a newer run.
- Each lambda runs in parallel and a failure does not cancel the others.
- Both can be triggered manually (`workflow_dispatch`) to process all lambdas. A manual CD only works from `main`, because the AWS role trusts that branch only.

## GitHub configuration (Settings > Secrets and variables > Actions)

| Type | Name | Purpose |
| --- | --- | --- |
| Secret | `AWS_ROLE_ARN` | Deploy role of the lambda CD (output `deploy_role_arn`) |
| Secret | `AWS_PLAN_ROLE_ARN` | Read-only role for `terraform plan` on PRs (output `plan_role_arn`) |
| Secret | `AWS_APPLY_ROLE_ARN` | Role for `terraform apply`, assumable from the `prod` environment only (output `apply_role_arn`) |
| Variable | `AWS_REGION` | **DR region** (same value as `dr_region` in the infra repository), used for ECR and Lambda |
| Variable | `ECR_REPOSITORY_PREFIX` | Prefix of the ECR repositories, e.g. `coika/disaster_recovery` |
| Variable | `LAMBDA_NAME_PREFIX` | Prefix of the function names, e.g. `coika` (same as `name_prefix` in the infra repository) |

Environment `prod` (Settings > Environments) with required reviewers: the apply job waits for their approval.

## AWS roles and first apply

The three roles are defined in `infra/iam.tf`, so this repo does not depend on any other repository for its pipeline:

- **deploy** (`AWS_ROLE_ARN`): the `main` branch only; pushes images to the ECR repositories and runs `lambda:UpdateFunctionCode` / `lambda:GetFunctionConfiguration` on the functions.
- **plan**: pull requests only; read-only plus the state lock.
- **apply**: environment `prod` only; manages the ECR repositories and reads/writes only `lambdas/terraform.tfstate`. It has no IAM write permissions, so the pipeline can never change the roles.

A pipeline needs its roles to exist, so the **first apply is done by hand** from a workstation with admin credentials, and so is any later change to `iam.tf` (a PR that changes a role fails at apply with AccessDenied):

```powershell
cd terraform
terraform init
terraform apply -var dr_region=eu-central-1
```

Then copy the outputs (`deploy_role_arn`, `plan_role_arn`, `apply_role_arn`) to the three secrets above. The GitHub OIDC provider is looked up (one per AWS account); set `create_github_oidc_provider = true` if the account has none.

## Lambda environment variables

Set by Terraform in the infra repository, not here.

- `auth_service/health_check`: `HEALTH_URL` (required), URL of the backend health endpoint.
- `auth_service/primary_recovery_check`: `STATE_TABLE` (failover state table), `SNS_TOPIC_ARN`, `PRIMARY_REGION` and `PROBE_SERVICES` (required); `PRIMARY_HEALTH_URL` (optional, health endpoint of the primary backend), `RECOVERY_THRESHOLD` (default 12 consecutive healthy runs), `SERVICE_KEY` (default `auth_service`).
- `auth_service/restore_db`: `DB_INSTANCE_NAME` (required), `Name` tag of the DB host; `MAX_WAIT_SECONDS` (optional, default 2700), total time to wait for a restore.

## License

[MIT](LICENSE) (c) 2026 Alejandro CortÃ©s Cabrejas.
