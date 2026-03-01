variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-central-1"
}

variable "instance_type" {
  description = "EC2 instance type backing the ECS cluster"
  type        = string
  default     = "t3.small"
}

variable "allowed_cidrs" {
  description = "CIDR blocks allowed to access Jarvis ports (e.g. your home IP). No SSH needed — use SSM."
  type        = list(string)
  # No default — forces explicit input to avoid accidental 0.0.0.0/0
}

variable "jarvis_image" {
  description = "Docker image to run"
  type        = string
  default     = "ghcr.io/rsprudencio/jarvis:latest"
}


variable "todoist_api_token" {
  description = "Todoist API token (leave empty to skip jarvis-todoist)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "volume_size" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 20
}

variable "name_prefix" {
  description = "Prefix for all resource names/tags"
  type        = string
  default     = "jarvis"
}
