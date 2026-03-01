.PHONY: init plan apply destroy ca-cert ca-key client-cert mcp-config ssm exec health logs

init:
	terraform init

plan: init
	terraform plan

apply: init
	terraform apply

destroy:
	terraform destroy

# --- Post-deploy helpers ---

# Save CA cert locally (needed for NODE_EXTRA_CA_CERTS)
ca-cert:
	@terraform output -raw ca_cert > ca.crt
	@echo "Saved to ca.crt"
	@echo "Add to shell: export NODE_EXTRA_CA_CERTS=$$(pwd)/ca.crt"

# Extract CA key for local client cert generation
ca-key:
	@terraform output -raw ca_key > ca.key
	@chmod 600 ca.key
	@echo "Saved ca.key (KEEP SECRET — used to sign client certs)"

# Generate client cert locally (never touches Terraform state)
# Usage: make client-cert USER=raph
client-cert:
	@test -f ca.key || (echo "Run 'make ca-key' first" && exit 1)
	@test -f ca.crt || (echo "Run 'make ca-cert' first" && exit 1)
	@../../docker/jarvis-certs.sh --client $(USER) .
	@echo ""
	@echo "Configure Claude Code:"
	@echo "  export CLAUDE_CODE_CLIENT_CERT=$$(pwd)/$(USER).crt"
	@echo "  export CLAUDE_CODE_CLIENT_KEY=$$(pwd)/$(USER).key"
	@echo "  export NODE_EXTRA_CA_CERTS=$$(pwd)/ca.crt"

# Print MCP config template (same for all users — auth via client cert)
mcp-config:
	@terraform output -raw mcp_config | python3 -m json.tool

# SSM into the EC2 instance (host level)
ssm:
	@eval "$$(terraform output -raw ssm_command)"

# ECS Exec into the running Jarvis container
exec:
	@TASK_ARN=$$(aws ecs list-tasks --cluster $$(terraform output -raw public_ip | sed 's/.*/jarvis/') --query 'taskArns[0]' --output text --region $$(terraform output -raw ssm_command | grep -oP '(?<=--region )\S+')) && \
	aws ecs execute-command --cluster jarvis --task $$TASK_ARN --container jarvis --interactive --command /bin/bash

# Check health (requires ca.crt extracted first)
health:
	@curl -sf --cacert ca.crt https://$$(terraform output -raw public_ip):8741/health | python3 -m json.tool

# Tail container logs from CloudWatch
logs:
	@eval "$$(terraform output -raw logs_command)"
