"""Manual test: run async Textract on one S3 object and print the start of the text.

Usage: python scripts/textract_smoke.py <bucket> <key>
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.steps import start_text_detection, wait_for_text

bucket, key = sys.argv[1], sys.argv[2]
job_id = start_text_detection(bucket, key)
print("job:", job_id)
status, text = wait_for_text(job_id)
print("status:", status)
print((text or "")[:800])
