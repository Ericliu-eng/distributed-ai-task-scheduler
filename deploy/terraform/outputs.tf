output "url" {
  description = "Public HTTPS address of the demo. The certificate is issued a minute or two after boot."
  value       = "https://${local.site_address}"
}

output "public_ip" {
  value = aws_eip.site.public_ip
}

output "instance_id" {
  value = aws_instance.demo.id
}

output "shell" {
  description = "Open a shell on the instance through SSM (requires the Session Manager plugin)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.demo.id}"
}
