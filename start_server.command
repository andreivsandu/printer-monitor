#!/bin/bash

# 1. Dynamically go to the exact folder where this script lives (Perfect for server migration!)
cd "$(dirname "$0")"
mkdir -p runtime

# 2. Kill any old servers, then start a new one on port 8000
lsof -ti:8000 | xargs kill -9 2>/dev/null
python3 -m http.server 8000 &

echo "======================================================="
echo "🌐 LOCAL SERVER RUNNING!"
echo "👉 Link for your browser/Notion: http://localhost:8000/runtime/printer_dashboard.html"
echo "⚠️  DO NOT CLOSE THIS WINDOW. MINIMIZE IT."
echo "======================================================="

# 3. Infinite loop: Update data, wait 5 minutes, repeat.
while true; do
    echo "[$(date)] Fetching new printer data..."
    /usr/bin/python3 generate_dashboard.py
    
    echo "[$(date)] Update complete. Waiting 5 minutes..."
    sleep 300
done