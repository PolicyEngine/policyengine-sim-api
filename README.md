# PolicyEngine API v2

Monorepo for PolicyEngine's API infrastructure, containing all services, libraries, and deployment configuration. 

## Quick start

### Prerequisites

- Docker and Docker Compose
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- gcloud CLI (for deployment)
- Terraform 1.5+ (for deployment)

### Local development

Start all services:
```bash
make up        # Start services on ports 8081-8083
make logs      # View logs
make down      # Stop services
```

Run the test suite:
```bash
make test                          # Unit tests only
make test-integration-with-services # Full integration tests (manages services automatically)
make test-complete                 # Everything: unit + integration tests
```

## Architecture

The repository contains three main API services:

- **api-full** (port 8081): Main PolicyEngine API with household calculations
- **simulation-executor** (port 8082): Economic simulation engine  
- **api-tagger** (port 8083): Cloud Run revision management

Each service generates OpenAPI specs and Python client libraries for integration testing.

## Development workflow

### Making changes

1. Edit code locally - services hot-reload automatically when running via `make up`
2. Run tests: `make test-complete`
3. Commit changes to a feature branch
4. Open a PR - GitHub Actions will run tests automatically

### Testing

Unit tests run in isolated containers:
```bash
make test                    # All services
make test-service service=api-full  # Single service
```

Integration tests use generated client libraries:
```bash
make generate-clients        # Generate OpenAPI clients (done automatically by test commands)
make test-integration        # Run integration tests (requires services running)
```

### Project structure

```
/
├── projects/               # Service applications
│   ├── policyengine-api-full/
│   ├── policyengine-simulation-executor/
│   ├── policyengine-api-tagger/
│   └── policyengine-apis-integ/    # Integration tests
├── libs/                   # Shared libraries
│   └── policyengine-fastapi/       # Common FastAPI utilities
├── deployment/             # Deployment configuration
│   ├── docker-compose.yml          # Local development
│   ├── docker-compose.prod.yml     # Production builds
│   └── terraform/                  # Infrastructure as code
├── scripts/                # Utility scripts
└── .github/workflows/      # CI/CD pipelines
```

## Deployment

### Setting up a new GCP project

**Important**: Most development should be done locally. Cloud deployment is slow and harder to debug.

1. Configure environment:
```bash
cp deployment/.env.example deployment/.env
# Edit deployment/.env with your GCP project details
```

2. Deploy infrastructure:
```bash
make deploy  # Builds images, pushes to registry, runs terraform
```

For existing GCP projects with resources:
```bash
make terraform-import  # Import existing resources
./deployment/terraform/handle-existing-workflows.sh $PROJECT_ID --delete  # Handle workflows
```

See [deployment guide](deployment/DEPLOYMENT_GUIDE.md) for detailed instructions.

### GitHub Actions deployment

The repository includes automated deployment pipelines:

1. **Pull requests**: Runs tests and builds
2. **Merge to main**: 
   - Deploys to beta environment
   - Runs integration tests
   - Deploys to production
   - Publishes API client packages to PyPI

Configure GitHub environments with these variables:
- `PROJECT_ID`: GCP project ID
- `REGION`: GCP region (usually us-central1)
- `_GITHUB_IDENTITY_POOL_PROVIDER_NAME`: Workload identity provider

### Important notes from experience

- **Wait after bootstrap**: GCP permission propagation can take up to an hour
- **Workflows can't be imported**: Use the provided script to handle existing workflows
- **Always test locally first**: Cloud debugging is painful
- **Check terraform state**: If deployments fail, check if resources already exist

## Commands reference

### Development
- `make up` - Start services locally
- `make down` - Stop services
- `make logs` - View service logs
- `make build` - Build Docker images

### Testing
- `make test` - Run unit tests
- `make test-integration` - Run integration tests
- `make test-complete` - Run all tests with service management
- `make generate-clients` - Generate API client libraries

### Deployment
- `make deploy` - Full deployment to GCP
- `make terraform-plan` - Preview infrastructure changes
- `make terraform-import` - Import existing resources
- `make terraform-destroy` - Remove all infrastructure
- `make publish-clients` - Publish API clients to PyPI (requires PYPI_TOKEN)

## Troubleshooting

### Services won't start
- Check Docker is running
- Ensure ports 8081-8083 are free
- Run `make build` to rebuild images

### Integration tests fail
- Regenerate clients: `make generate-clients`
- Check services are healthy: `make logs`
- Verify port configuration matches docker-compose.yml

### Deployment issues
- Check deployment/.env configuration
- Verify GCP authentication: `gcloud auth list`
- For "already exists" errors: `make terraform-import`
- For workflow errors: `./deployment/terraform/handle-existing-workflows.sh`

## Contributing

See [AGENTS.md](AGENTS.md) and
[docs/engineering/skills/github-prs.md](docs/engineering/skills/github-prs.md)
for the canonical same-repository draft PR workflow.

1. Create a feature branch
2. Open a GitHub issue for non-trivial work
3. Make changes and test locally
4. Ensure `make test-complete` passes when feasible
5. Open a same-repository draft PR with `Fixes #ISSUE_NUMBER` as the first line
   of the description
6. Include a clear summary and testing notes
7. Wait for CI checks to pass
