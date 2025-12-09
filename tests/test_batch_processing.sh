#!/bin/bash
# Test batch processing mode - outputs annotated video + JSON metadata

cd "$(dirname "$0")/.."

echo "=========================================="
echo "Batch Processing Test"
echo "=========================================="

# Create output directory
OUTPUT_DIR="batch_output_test"
rm -rf $OUTPUT_DIR
mkdir -p $OUTPUT_DIR

echo ""
echo "Processing cala_del_moral.ts in batch mode"
echo "Output directory: $OUTPUT_DIR"

# Run detector in batch mode (direct file input)
python3 -m src.main \
  --input-srt ../cala_del_moral.ts \
  --batch-output $OUTPUT_DIR \
  --model models/yolov8n.pt \
  --conf 0.25

echo ""
echo "=========================================="
echo "Batch Processing Complete!"
echo "=========================================="
echo "Output files (named after input file):"
ls -lh $OUTPUT_DIR/
echo ""
echo "View video: ffplay $OUTPUT_DIR/cala_del_moral.mp4"
echo "View JSON:  cat $OUTPUT_DIR/cala_del_moral.json | jq '.video_info'"
