output "public_ip" {
  description = "Elastic IP of the Jarvis instance (stable across restarts)"
  value       = aws_eip.jarvis.public_ip
}

output "ssm_command" {
  description = "SSM command to access the EC2 instance"
  value       = "aws ssm start-session --target ${aws_instance.jarvis.id} --region ${var.aws_region}"
}

output "ecs_exec_command" {
  description = "ECS Exec command to shell into the running Jarvis container"
  value       = "aws ecs execute-command --cluster ${var.name_prefix} --task $(aws ecs list-tasks --cluster ${var.name_prefix} --query 'taskArns[0]' --output text --region ${var.aws_region}) --container jarvis --interactive --command /bin/bash --region ${var.aws_region}"
}

output "health_check" {
  description = "Health check command (requires ca.crt extracted via 'make ca-cert')"
  value       = "curl -sf --cacert ca.crt https://${aws_eip.jarvis.public_ip}:8741/health"
}

output "ca_cert" {
  description = "CA certificate PEM (save to file for NODE_EXTRA_CA_CERTS)"
  value       = tls_self_signed_cert.ca.cert_pem
  sensitive   = true
}

output "ca_key" {
  description = "CA private key PEM (for local client cert generation via jarvis-certs.sh --client)"
  value       = tls_private_key.ca.private_key_pem
  sensitive   = true
}

output "mcp_config" {
  description = "MCP config template for Claude Code .mcp.json (same for all users — auth via client cert)"
  value = jsonencode({
    mcpServers = {
      "jarvis-core" = {
        type = "streamable-http"
        url  = "https://${aws_eip.jarvis.public_ip}:8741/mcp"
      }
      "jarvis-todoist" = {
        type = "streamable-http"
        url  = "https://${aws_eip.jarvis.public_ip}:8742/mcp"
      }
      "jarvis-obsidian" = {
        type = "streamable-http"
        url  = "https://${aws_eip.jarvis.public_ip}:8744/mcp"
      }
    }
  })
}

output "logs_command" {
  description = "CloudWatch Logs tail command"
  value       = "aws logs tail /ecs/${var.name_prefix} --follow --region ${var.aws_region}"
}
