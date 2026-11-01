#!/bin/bash
BUCKET="docpipe-uploads-746491202337"
TABLE="docpipe-documents"
COUNT=20

echo "=== Uploading $COUNT documents ==="
for i in $(seq -w 1 $COUNT); do
  cat > /tmp/load-${i}.txt << EOF
Account Statement #LOAD-${i}
Account holder: Test User ${i}
Reference: REF-${RANDOM}
Balance: \$$(( RANDOM % 9999 + 100 )).$(( RANDOM % 99 ))
Date: 2026-01-${i}
Type: Monthly Statement
EOF
  aws s3 cp /tmp/load-${i}.txt "s3://${BUCKET}/load-${i}.txt" --quiet
done
echo "All $COUNT uploaded. Waiting 3 minutes for processing..."
sleep 180

echo ""
echo "=== Results ==="
completed=0
review=0
failed=0
processing=0

for i in $(seq -w 1 $COUNT); do
  status=$(MSYS_NO_PATHCONV=1 aws dynamodb get-item \
    --table-name "$TABLE" \
    --key "{\"document_id\":{\"S\":\"load-${i}.txt\"}}" \
    --projection-expression "#s" \
    --expression-attribute-names '{"#s":"status"}' \
    --query "Item.status.S" --output text 2>/dev/null)
  case "$status" in
    COMPLETED)  ((completed++)) ;;
    REVIEW)     ((review++)) ;;
    FAILED)     ((failed++)) ;;
    *)          ((processing++)); echo "  load-${i}.txt: $status" ;;
  esac
done

echo "COMPLETED: $completed"
echo "REVIEW:    $review"
echo "FAILED:    $failed"
echo "PROCESSING/OTHER: $processing"
echo ""
if [ $processing -eq 0 ] && [ $failed -eq 0 ]; then
  echo "✓ Load test PASSED — all documents reached a terminal state"
else
  echo "⚠ Some documents did not reach a terminal state"
fi
