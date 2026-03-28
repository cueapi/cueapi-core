#!/bin/bash
# Basic CueAPI usage example
# Prerequisites: CueAPI running at http://localhost:8000

BASE_URL="http://localhost:8000"

# Step 1: Register and get your API key
curl -s -X POST $BASE_URL/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'

# Set your API key from the magic link response
API_KEY="cue_sk_your_key"

# Step 2: Create a recurring cue
curl -s -X POST $BASE_URL/v1/cues \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "morning-agent-brief",
    "schedule": {
      "type": "recurring",
      "cron": "0 9 * * *",
      "timezone": "America/Los_Angeles"
    },
    "callback": {
      "url": "https://your-agent.com/run"
    },
    "payload": {
      "task": "daily_brief"
    }
  }'

# Step 3: List your cues
curl -s $BASE_URL/v1/cues \
  -H "Authorization: Bearer $API_KEY"

# Step 4: Check execution history
curl -s $BASE_URL/v1/executions \
  -H "Authorization: Bearer $API_KEY"
