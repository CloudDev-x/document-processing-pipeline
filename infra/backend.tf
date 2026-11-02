terraform {
  backend "s3" {
    bucket       = "docpipe-tfstate-746491202337"
    key          = "docpipe/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
