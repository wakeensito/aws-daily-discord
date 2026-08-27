{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormation",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateChangeSet",
        "cloudformation:CreateStack",
        "cloudformation:DeleteChangeSet",
        "cloudformation:DescribeChangeSet",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResource",
        "cloudformation:DescribeStackResources",
        "cloudformation:DescribeStacks",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:GetTemplate",
        "cloudformation:TagResource",
        "cloudformation:UpdateStack"
      ],
      "Resource": [
        "arn:aws:cloudformation:${region}:${account_id}:stack/${prefix}-*/*",
        "arn:aws:cloudformation:${region}:${account_id}:changeSet/*/*"
      ]
    },
    {
      "Sid": "CloudFormationServerlessTransform",
      "Effect": "Allow",
      "Action": "cloudformation:CreateChangeSet",
      "Resource": "arn:aws:cloudformation:${region}:aws:transform/Serverless-2016-10-31"
    },
    {
      "Sid": "CloudFormationGlobal",
      "Effect": "Allow",
      "Action": [
        "cloudformation:ListStacks",
        "cloudformation:GetTemplateSummary",
        "cloudformation:ValidateTemplate"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteObject",
        "s3:GetBucketLocation",
        "s3:GetBucketPolicy",
        "s3:GetObject",
        "s3:ListBucket",
        "s3:PutBucketEncryption",
        "s3:PutBucketPolicy",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketTagging",
        "s3:PutBucketVersioning",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::${prefix}-*",
        "arn:aws:s3:::${prefix}-*/*",
        "arn:aws:s3:::aws-sam-cli-managed-default-*",
        "arn:aws:s3:::aws-sam-cli-managed-default-*/*"
      ]
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": [
        "lambda:AddPermission",
        "lambda:CreateFunction",
        "lambda:CreateFunctionUrlConfig",
        "lambda:DeleteFunction",
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:ListTags",
        "lambda:PublishVersion",
        "lambda:RemovePermission",
        "lambda:TagResource",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration"
      ],
      "Resource": [
        "arn:aws:lambda:${region}:${account_id}:function:${prefix}-*",
        "arn:aws:lambda:${region}:${account_id}:function:${prefix}-*:*"
      ]
    },
    {
      "Sid": "APIGateway",
      "Effect": "Allow",
      "Action": [
        "apigateway:DELETE",
        "apigateway:GET",
        "apigateway:PATCH",
        "apigateway:POST",
        "apigateway:PUT"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:CreateTable",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:TagResource",
        "dynamodb:UpdateTable"
      ],
      "Resource": "arn:aws:dynamodb:${region}:${account_id}:table/${prefix}-*"
    },
    {
      "Sid": "CloudFront",
      "Effect": "Allow",
      "Action": [
        "cloudfront:CreateDistribution",
        "cloudfront:CreateInvalidation",
        "cloudfront:CreateOriginAccessControl",
        "cloudfront:GetDistribution",
        "cloudfront:GetOriginAccessControl",
        "cloudfront:ListDistributions",
        "cloudfront:TagResource",
        "cloudfront:UpdateDistribution",
        "cloudfront:UpdateOriginAccessControl"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "logs:PutRetentionPolicy",
        "logs:TagResource"
      ],
      "Resource": "arn:aws:logs:${region}:${account_id}:log-group:*"
    },
    {
      "Sid": "IAMRoleManagement",
      "Effect": "Allow",
      "Action": [
        "iam:AttachRolePolicy",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:DeleteRolePolicy",
        "iam:DetachRolePolicy",
        "iam:GetRole",
        "iam:GetRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:PutRolePolicy",
        "iam:TagRole"
      ],
      "Resource": "arn:aws:iam::${account_id}:role/${prefix}-*"
    },
    {
      "Sid": "IAMPassRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::${account_id}:role/${prefix}-*",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": [
            "lambda.amazonaws.com",
            "apigateway.amazonaws.com",
            "cloudformation.amazonaws.com"
          ]
        }
      }
    }
  ]
}
