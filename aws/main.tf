resource "aws_cloudwatch_log_group" "api" {
  name = "/nl2sql/api"
}

resource "aws_db_parameter_group" "app" {
  name   = "nl2sql-pg16"
  family = "postgres16"

  parameter {
    name         = "shared_preload_libraries"
    value        = "vector"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "app" {
  identifier              = "nl2sql"
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = "db.t3.micro"
  allocated_storage       = 20
  max_allocated_storage   = 20
  db_name                 = "postgres"
  username                = "postgres"
  password                = "postgres"
  parameter_group_name    = aws_db_parameter_group.app.name
  publicly_accessible     = true
  skip_final_snapshot     = true
  backup_retention_period = 0
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id      = "nl2sql"
  engine          = "redis"
  node_type       = "cache.t3.micro"
  num_cache_nodes = 1
  port            = 6379

  # MiniStack reports the published host port here. That value is not the
  # port in this block, and changing it would replace the cache.
  lifecycle {
    ignore_changes = [port, auto_minor_version_upgrade]
  }
}

locals {
  db_host        = aws_db_instance.app.address
  db_port        = aws_db_instance.app.port
  redis_endpoint = aws_elasticache_cluster.redis.cache_nodes[0].address
  redis_port     = aws_elasticache_cluster.redis.cache_nodes[0].port
  secrets = {
    REDIS_URL             = "redis://${local.redis_endpoint}:${local.redis_port}/0"
    NL2SQL_CHECKPOINT_DSN = "postgresql://nl2sql_checkpoint:pw@${local.db_host}:${local.db_port}/checkpoints"
    NL2SQL_MEMORY_DSN     = "postgresql://nl2sql_memory:pw@${local.db_host}:${local.db_port}/memory"
    NL2SQL_AUTH_DSN       = "postgresql://nl2sql_auth:pw@${local.db_host}:${local.db_port}/auth"
    NL2SQL_ANALYST_DSN    = "postgresql://nl2sql_analyst:pw@${local.db_host}:${local.db_port}/app"
    NL2SQL_OPS_DSN        = "postgresql://nl2sql_ops:pw@${local.db_host}:${local.db_port}/app"
    NL2SQL_AUDIT_DSN      = "postgresql://nl2sql_audit:pw@${local.db_host}:${local.db_port}/audit"
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each = local.secrets
  name     = each.key
}

resource "aws_secretsmanager_secret_version" "app" {
  for_each      = local.secrets
  secret_id     = aws_secretsmanager_secret.app[each.key].id
  secret_string = each.value
}

resource "aws_iam_role" "exec" {
  name = "nl2sql-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_ecs_cluster" "api" {
  name = "nl2sql"
}

resource "aws_ecs_task_definition" "api" {
  family                   = "nl2sql-api"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.exec.arn
  container_definitions = jsonencode([{
    name      = "api"
    image     = "nl2sql-api:local"
    essential = true
    environment = [
      { name = "NVIDIA_API_KEY", value = "local-proof" },
    ]
    secrets = [
      for name, secret in aws_secretsmanager_secret.app : {
        name      = name
        valueFrom = secret.arn
      }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = "us-east-1"
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "nl2sql-api"
  cluster         = aws_ecs_cluster.api.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "EC2"

  # MiniStack returns an empty network configuration for this service, and the
  # AWS provider crashes while reading that empty object. A stored awsvpc
  # configuration gives the read a subnet and a security group to flatten.
  # The task still uses the host network.
  network_configuration {
    subnets          = ["subnet-nl2sql"]
    security_groups  = ["sg-nl2sql"]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [availability_zone_rebalancing]
  }
}
