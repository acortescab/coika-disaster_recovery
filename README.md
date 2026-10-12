# coika-disaster_recovery

Code of the lambdas used by the AWS disaster recovery of the Coika services. Each lambda is packaged as a container image, validated in CI (ruff + pytest) and pushed to ECR when merged to `main`.

The infrastructure that creates and wires them (ECR repositories, functions, IAM, alarms, Step Functions, CodeBuild) lives in the sibling repository **coika-disaster_recovery-infra**. This repo only builds and deploys code.

## Structure

```
lambdas/<service>/<lambda>/    # one folder per lambda (app.py, Dockerfile, requirements*.txt, tests/)
.github/
  workflows/ci.yml             # on PR: detects changed lambdas and runs lambda-ci.yml in a matrix
  workflows/lambda-ci.yml      # reusable: lint, test and build (no push) for ONE lambda
  workflows/cd.yml             # on push to main: detects changed lambdas and runs lambda-cd.yml in a matrix
  workflows/lambda-cd.yml      # reusable: build, push to ECR and update the function for ONE lambda
  scripts/changed_lambdas.py   # computes the matrix of affected lambdas
ruff.toml                      # shared lint config
```

Current lambdas:

| Lambda | Description |
| --- | --- |
| `auth_service/health_check` | Sends a GET to `HEALTH_URL` and publishes the `BackendHealth` metric to CloudWatch (`Coika/DisasterRecovery`). |
| `auth_service/restore_db` | Called by the failover state machine after the deploy: runs `restore.sh --yes --if-empty` on the DB host of the DR region through SSM and waits for it. It returns `InProgress` if the restore outlasts the invocation, and the state machine calls it again. |

## Adding a new lambda

1. Create `lambdas/<service>/<name>/` with `app.py`, `Dockerfile`, `requirements.txt`, `requirements-dev.txt` and `tests/` (copy an existing one).
2. In the infra repository, add a `module "lambda_container"` call for it (it creates the ECR repository `<ECR_REPOSITORY_PREFIX>/<service>/<name>` and the function `<LAMBDA_NAME_PREFIX>-<service>-<name>`) and apply it **before** merging here: the CD updates a function that must already exist.

No YAML needs to be touched: `ci.yml` and `cd.yml` discover any `lambdas/*/*/` folder that has a `Dockerfile`. The repository and function names are computed in `changed_lambdas.py` and must stay in sync with `modules/lambda_container` of the infra repository.

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
| Secret | `AWS_ROLE_ARN` | IAM role assumed by the workflow via OIDC (output of the infra repository) |
| Variable | `AWS_REGION` | **DR region** (same value as `dr_region` in the infra repository), used for ECR and Lambda |
| Variable | `ECR_REPOSITORY_PREFIX` | Prefix of the ECR repositories, e.g. `coika/disaster_recovery` |
| Variable | `LAMBDA_NAME_PREFIX` | Prefix of the function names, e.g. `coika` (same as `name_prefix` in the infra repository) |

The role is created by the infra repository (`github_oidc.tf`): it trusts the `main` branch of this repository only, and can push to the ECR repositories and run `lambda:UpdateFunctionCode` / `lambda:GetFunctionConfiguration` on the functions.

## Lambda environment variables

Set by Terraform in the infra repository, not here.

- `auth_service/health_check`: `HEALTH_URL` (required), URL of the backend health endpoint.
- `auth_service/restore_db`: `DB_INSTANCE_NAME` (required), `Name` tag of the DB host; `MAX_WAIT_SECONDS` (optional, default 2700), total time to wait for a restore.

## License

[MIT](LICENSE) (c) 2026 Alejandro Cortés Cabrejas.
