variable "region" {
  description = "AWS region for the demo."
  type        = string
  default     = "us-west-2"
}

variable "instance_type" {
  description = "EC2 instance type. The AMI is arm64, so this must be a Graviton type."
  type        = string
  default     = "t4g.small"
}

variable "root_volume_gb" {
  description = "Encrypted gp3 root volume size."
  type        = number
  default     = 20
}

variable "repo_url" {
  description = "Git repository the instance clones at boot."
  type        = string
  default     = "https://github.com/Ericliu-eng/distributed-ai-task-scheduler.git"
}

variable "git_ref" {
  description = "Branch or tag the instance deploys."
  type        = string
  default     = "main"
}

variable "budget_limit_usd" {
  description = "Monthly cost budget. Email alerts fire at 80% actual and 100% forecast."
  type        = number
  default     = 20
}

variable "budget_email" {
  description = "Address that receives budget alerts."
  type        = string
}
