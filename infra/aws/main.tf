terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ---------- Data Sources ----------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# ECS-optimized Amazon Linux 2023 AMI (Docker + ECS agent + SSM agent pre-installed)
data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

# ---------- IAM: EC2 Instance Role (ECS agent + SSM) ----------

resource "aws_iam_role" "ecs_instance" {
  name = "${var.name_prefix}-ecs-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_instance_ecs" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "ecs_instance_ssm" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ecs" {
  name = "${var.name_prefix}-ecs-profile"
  role = aws_iam_role.ecs_instance.name
}

# ---------- IAM: ECS Task Execution Role (pull image, write logs) ----------

resource "aws_iam_role" "task_execution" {
  name = "${var.name_prefix}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------- IAM: ECS Task Role (SSM for ECS Exec) ----------

resource "aws_iam_role" "task" {
  name = "${var.name_prefix}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "task_ssm" {
  name = "${var.name_prefix}-task-ssm"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.jarvis.arn}:*"
      },
    ]
  })
}

# ---------- CloudWatch Logs ----------

resource "aws_cloudwatch_log_group" "jarvis" {
  name              = "/ecs/${var.name_prefix}"
  retention_in_days = 30
}

# ---------- Security Group ----------

resource "aws_security_group" "jarvis" {
  name_prefix = "${var.name_prefix}-"
  description = "Jarvis MCP server access (no SSH — use SSM)"
  vpc_id      = data.aws_vpc.default.id

  # jarvis-core
  ingress {
    description = "jarvis-core"
    from_port   = 8741
    to_port     = 8741
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  # jarvis-todoist
  ingress {
    description = "jarvis-todoist"
    from_port   = 8742
    to_port     = 8742
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  # jarvis-obsidian
  ingress {
    description = "jarvis-obsidian"
    from_port   = 8744
    to_port     = 8744
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  # All outbound (GHCR pulls, SSM endpoints, etc.)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-sg" }
}

# ---------- TLS Certificates (self-signed) ----------

resource "tls_private_key" "ca" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem = tls_private_key.ca.private_key_pem

  subject {
    common_name = "Jarvis CA"
  }

  is_ca_certificate     = true
  validity_period_hours = 87600 # 10 years

  allowed_uses = ["cert_signing", "crl_signing"]
}

resource "tls_private_key" "server" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "server" {
  private_key_pem = tls_private_key.server.private_key_pem

  subject {
    common_name = "jarvis-server"
  }

  ip_addresses = [aws_eip.jarvis.public_ip, "127.0.0.1"]
  dns_names    = ["localhost"]
}

resource "tls_locally_signed_cert" "server" {
  cert_request_pem   = tls_cert_request.server.cert_request_pem
  ca_private_key_pem = tls_private_key.ca.private_key_pem
  ca_cert_pem        = tls_self_signed_cert.ca.cert_pem

  validity_period_hours = 8760 # 1 year

  allowed_uses = ["digital_signature", "key_encipherment", "server_auth"]
}

# ---------- Elastic IP (stable address — survives task restarts) ----------

resource "aws_eip" "jarvis" {
  domain = "vpc"
  tags   = { Name = "${var.name_prefix}-eip" }
}

resource "aws_eip_association" "jarvis" {
  instance_id   = aws_instance.jarvis.id
  allocation_id = aws_eip.jarvis.id
}

# ---------- EC2 Instance (ECS-optimized, SSM-enabled) ----------

resource "aws_instance" "jarvis" {
  ami                    = data.aws_ssm_parameter.ecs_ami.value
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.ecs.name
  vpc_security_group_ids = [aws_security_group.jarvis.id]
  subnet_id              = data.aws_subnets.default.ids[0]

  # No key_name — access via SSM only

  root_block_device {
    volume_size = var.volume_size
    volume_type = "gp3"
  }

  user_data = templatefile("${path.module}/userdata.sh.tftpl", {
    ecs_cluster       = var.name_prefix
    todoist_api_token = var.todoist_api_token
    ca_key_pem        = tls_private_key.ca.private_key_pem
    ca_cert_pem       = tls_self_signed_cert.ca.cert_pem
    server_key_pem    = tls_private_key.server.private_key_pem
    server_cert_pem   = tls_locally_signed_cert.server.cert_pem
  })

  user_data_replace_on_change = true

  tags = { Name = "${var.name_prefix}-ecs-instance" }
}

# ---------- ECS Cluster ----------

resource "aws_ecs_cluster" "jarvis" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------- ECS Task Definition ----------

resource "aws_ecs_task_definition" "jarvis" {
  family                   = var.name_prefix
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # Host bind mounts — directories created by user_data
  volume {
    name      = "vault"
    host_path = "/opt/jarvis/vault"
  }

  volume {
    name      = "config"
    host_path = "/opt/jarvis/config"
  }

  volume {
    name      = "certs"
    host_path = "/opt/jarvis/certs"
  }

  container_definitions = jsonencode([{
    name      = "jarvis"
    image     = var.jarvis_image
    essential = true
    memory    = 1536 # MB — leaves room for ECS agent on t3.small (2GB)

    portMappings = [
      { containerPort = 8741, hostPort = 8741, protocol = "tcp" },
      { containerPort = 8742, hostPort = 8742, protocol = "tcp" },
      { containerPort = 8743, hostPort = 8743, protocol = "tcp" },
      { containerPort = 8744, hostPort = 8744, protocol = "tcp" },
    ]

    mountPoints = [
      { sourceVolume = "vault", containerPath = "/vault", readOnly = false },
      { sourceVolume = "config", containerPath = "/config", readOnly = false },
      { sourceVolume = "certs", containerPath = "/certs", readOnly = true },
    ]

    environment = [
      { name = "JARVIS_HOME", value = "/config" },
      { name = "JARVIS_VAULT_PATH", value = "/vault" },
      { name = "CHROMA_HOST", value = "127.0.0.1" },
      { name = "CHROMA_PORT", value = "8743" },
      { name = "JARVIS_TLS_CERT", value = "/certs/server.crt" },
      { name = "JARVIS_TLS_KEY", value = "/certs/server.key" },
      { name = "JARVIS_TLS_CA", value = "/certs/ca.crt" },
    ]

    healthCheck = {
      command     = ["CMD", "curl", "-sfk", "https://localhost:8741/health"]
      interval    = 30
      timeout     = 5
      startPeriod = 15
      retries     = 3
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.jarvis.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "jarvis"
      }
    }
  }])
}

# ---------- ECS Service (with ECS Exec for SSM access) ----------

resource "aws_ecs_service" "jarvis" {
  name            = var.name_prefix
  cluster         = aws_ecs_cluster.jarvis.id
  task_definition = aws_ecs_task_definition.jarvis.arn
  desired_count   = 1
  launch_type     = "EC2"

  enable_execute_command = true

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  depends_on = [aws_instance.jarvis]
}
