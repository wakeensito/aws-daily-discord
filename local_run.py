"""Preview a Daily Cloud Fun Fact post locally — nothing touches Discord.

Runs the real pipeline (topic pick + Bedrock generation + assembly) with
your local AWS credentials, prints the finished post, and skips both the
webhook send and the rotation write.

    AWS_PROFILE=iamadmin python local_run.py
    AWS_PROFILE=iamadmin python local_run.py "Amazon S3"   # force a topic

Requires Bedrock model access for Nova Micro in the account (us-east-1).
"""

import os
import sys

# Must be set before the handler module reads them at import time.
os.environ["DRY_RUN"] = "1"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TOPICS_TABLE_NAME", "local-preview-no-table")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "http://localhost/unused-in-dry-run")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import lambda_function  # noqa: E402


def main():
    if len(sys.argv) > 1:
        wanted = sys.argv[1].lower()
        matches = [
            t for t in lambda_function.load_topics() if wanted in t["topic"].lower()
        ]
        if not matches:
            sys.exit(f"No topic matching {sys.argv[1]!r} in src/topics.json")
        entry = matches[0]
        content = lambda_function.generate_content(entry)
        print(lambda_function.assemble_message(entry, content))
    else:
        lambda_function.lambda_handler({}, None)


if __name__ == "__main__":
    main()
