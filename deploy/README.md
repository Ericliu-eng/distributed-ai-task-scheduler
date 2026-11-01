# Deploying the public demo

One small EC2 instance runs the whole stack with Docker Compose. Caddy terminates HTTPS in front of the API, PostgreSQL, two workers, and the recovery monitor. Only ports 80 and 443 are open; shell access goes through SSM Session Manager.

```
visitor ──HTTPS──▶ Caddy :443 ──▶ API :8000 ──▶ PostgreSQL
                                     ▲
                    workers + recovery monitor (internal network only)
```

## What makes it safe to leave public

| Guard | Setting | Effect |
| --- | --- | --- |
| Rate limit | `RATE_LIMIT_PER_MINUTE=20` | Each visitor IP may create 20 tasks or workflows a minute; more get HTTP 429 with `Retry-After` |
| Queue cap | `MAX_ACTIVE_TASKS=200` | New work is refused with HTTP 503 while 200 tasks are queued, running, or blocked |
| No shared reset | `ALLOW_DEMO_RESET=false` | One visitor cannot wipe what others are watching; the pipeline demos still work |
| Retention | `RETENTION_MINUTES=60` | Finished work and old events are deleted after an hour |
| Mock inference | — | No model API is called, so visitors cannot run up a provider bill |
| Request size | Caddy `max_size 64KB` | Oversized bodies are refused at the proxy |

The database password is generated on the instance at first boot. It is never in this repository or in Terraform state.

## Try the production stack locally

```bash
cp deploy/.env.example deploy/.env   # set POSTGRES_PASSWORD; keep SITE_ADDRESS=localhost
docker compose -f deploy/compose.prod.yml up -d --build
curl -k https://localhost/health
docker compose -f deploy/compose.prod.yml down -v
```

Caddy issues a certificate from its own local CA for `localhost`, so curl needs `-k` and a browser will warn. Set `HTTP_PORT` and `HTTPS_PORT` in `deploy/.env` if 80 or 443 are taken.

## Deploy to AWS

You need AWS credentials (`aws configure` or `aws login`) with permission to create EC2, IAM, and Budgets resources.

```bash
cd deploy/terraform
terraform init
terraform plan -var budget_email=you@example.com
terraform apply -var budget_email=you@example.com
```

`terraform output url` prints the address, for example `https://203-0-113-10.sslip.io`. The [sslip.io](https://sslip.io) name resolves to the instance's Elastic IP, so Caddy can get a Let's Encrypt certificate without a registered domain. The first boot installs Docker and builds the image, which takes a few minutes; progress is in `/var/log/cloud-init-output.log`.

To deploy a new version, open a shell with `terraform output shell`, then run:

```bash
cd /opt/orbit && sudo git pull && cd deploy && sudo docker compose -f compose.prod.yml up -d --build
```

## Cost

The defaults are a `t4g.small` instance, a 20 GB encrypted gp3 volume, and one public IPv4 address. At on-demand prices that is roughly $15–20 a month; check current EC2 and VPC pricing for your region before applying. AWS Budgets emails `budget_email` at 80% of `budget_limit_usd` (default $20) actual spend and at 100% forecast. Budgets warn; they do not stop spending.

To stop paying, destroy everything:

```bash
terraform destroy -var budget_email=you@example.com
```
