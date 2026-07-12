# GrugThink Slash Commands Reference

## User Commands

### `/verify`
**Description**: Verify the truthfulness of the previous message
**Usage**: `/verify`
**Permissions**: All users
**Details**: Analyzes the last user message in the channel and provides fact-checking

### `/grant-memory-access`
**Description**: Grant memory management permissions to a user (admin only)
**Usage**: `/grant-memory-access @user`
**Permissions**: Trusted admins only
**Details**: Allows a user to add/edit bot memories

### `/revoke-memory-access`
**Description**: Revoke memory management permissions from a user (admin only)
**Usage**: `/revoke-memory-access @user`
**Permissions**: Trusted admins only
**Details**: Removes memory management permissions from a user

### `/list-memory-managers`
**Description**: List users with memory management access (admin only)
**Usage**: `/list-memory-managers`
**Permissions**: Trusted admins only
**Details**: Shows all users who can manage bot memories

## Chat & Personality Commands

### `/get-chat-frequency`
**Description**: Get the bot's current natural chat frequency for this server
**Usage**: `/get-chat-frequency`
**Permissions**: Trusted admins only
**Details**: Returns the current chat frequency percentage

### `/chat-frequency`
**Description**: Set how often the bot naturally chats (0-100%)
**Usage**: `/chat-frequency 50`
**Permissions**: Trusted admins only
**Details**:
- 0% = Never chat naturally (default)
- 25% = Occasional natural chat
- 50% = Moderate engagement
- 75% = Frequent engagement
- 100% = Very chatty

### `/get-chat-settings`
**Description**: View current chat frequency and conversation settings
**Usage**: `/get-chat-settings`
**Permissions**: Trusted admins only
**Details**: Shows current natural chat settings and activity thresholds

### `/reset-activity`
**Description**: Reset activity tracking data for natural chat triggers
**Usage**: `/reset-activity`
**Permissions**: Trusted admins only
**Details**: Clears channel activity history used for intelligent conversation triggers

## Debug & Testing Commands

### `/ping`
**Description**: Test if the bot is responding
**Usage**: `/ping`
**Permissions**: All users
**Details**: Simple connectivity test to verify bot is working

### `/diagnose`
**Description**: Diagnose bot configuration and setup
**Usage**: `/diagnose`
**Permissions**: Trusted admins only
**Details**: 
- Checks API configuration (Gemini/Ollama)
- Tests database connectivity
- Validates bot settings
- Performs API test call

### `/test-response`
**Description**: Test bot response without AI
**Usage**: `/test-response`
**Permissions**: Trusted admins only
**Details**: Verifies basic bot mechanics work without AI calls

### `/test-bot-chat`
**Description**: Trigger a test bot conversation
**Usage**: `/test-bot-chat`
**Permissions**: Trusted admins only
**Details**: Forces an intelligent bot-to-bot conversation for testing

### `/test-natural-chat`
**Description**: Force test natural chat engagement
**Usage**: `/test-natural-chat`
**Permissions**: Trusted admins only
**Details**: Forces natural chat response generation with mock conversation data

### `/force-chat`
**Description**: Force bot to chat immediately
**Usage**: `/force-chat`
**Permissions**: Trusted admins only
**Details**: Bypasses all checks and makes bot send a simple test message

### `/repair-database`
**Description**: Repair corrupted database
**Usage**: `/repair-database`
**Permissions**: Trusted admins only
**Details**: 
- Backs up corrupted database file
- Creates fresh database
- Use when getting "disk I/O error" messages

## Natural Chat System

The bot has two main chat engagement systems:

### 1. Natural Chat Engagement
- Controlled by `/chat-frequency` setting
- Bot randomly engages based on conversation flow
- Analyzes recent messages, author count, and topics
- Requires chat frequency > 0% to work

### 2. Intelligent Bot Conversations
- Automatic based on activity thresholds:
  - 5min human silence + 10min bot silence = conversation trigger
  - 3min bot silence for joining active conversations  
  - 1-3% random engagement chance
- Cross-bot memory sharing for natural conversations
- Rate limited to 1 conversation per 10 minutes per channel

## Troubleshooting

### Bot Not Responding When Mentioned
1. Use `/ping` to test basic functionality
2. Use `/diagnose` to check API and database status
3. Check if API keys are configured properly
4. Use `/repair-database` if database errors appear

### Bot Not Chatting Naturally
1. Check chat frequency with `/get-chat-frequency` or `/get-chat-settings`
2. Set frequency with `/chat-frequency 50` (or desired %)
3. Reset activity tracking with `/reset-activity`
4. Use `/test-natural-chat` to force test

### Configuration Issues
- Ensure GEMINI_API_KEY environment variable is set
- Or configure OLLAMA_URLS for local AI
- Check TRUSTED_USER_IDS for admin commands
- Verify database directory has write permissions

## Permissions

**All Users**: `/verify`, `/ping`

**Trusted Admins Only**: All other commands
- Set via TRUSTED_USER_IDS environment variable
- Format: comma-separated Discord user IDs
- Example: `TRUSTED_USER_IDS=123456789,987654321`