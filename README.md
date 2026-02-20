# Nydus Tunnel

Nydus Tunnel is a Discord bot designed to monitor system resources, manage deployments, and provide an API for interacting with system services like Nginx. It is built using Python and leverages the Py-Cord library for Discord interactions.

## Features

- **System Monitoring**: Logs CPU, RAM, disk usage, and active connections.
- **Deployment Management**: Automates project deployments triggered via webhooks.
- **Nginx Management**: Provides commands to reload and check the status of Nginx.
- **API Endpoints**: Exposes RESTful APIs for stats, deployments, and Nginx operations.
- **Discord Notifications**: Sends alerts and logs to specified Discord channels.

## Setup

### Prerequisites
- Python 3.11+
- Git (optional, for version control)
- A Discord bot token

### Installation
1. Clone the repository (if using Git):
   ```bash
   git clone https://github.com/0xOptimizer/nydus.arvo.team_tunnel.git
   ```
2. Navigate to the project directory:
   ```bash
   cd nydus_tunnel
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Configuration
1. Create a `.env` file in the project root (or update the existing one):
   ```env
   NYDUS_BOT_TOKEN_ID=your_discord_bot_token_here
   DEV_ID=your_discord_user_id
   DEFAULT_OUTPUT_CHANNELS=[channel_id1, channel_id2]
   DB_PATH=./nydus.db
   PORT=4000
   ```
2. Replace placeholders with your actual values.

### Database Initialization
The database schema will be automatically initialized when the bot starts.

### Running the Bot
Start the bot with:
```bash
python main.py
```

## Usage

### Discord Commands
- **Monitoring Alerts**: High memory usage alerts are sent to the configured channels.
- **Deployment Logs**: Deployment statuses are logged and sent to Discord.

### API Endpoints
- **GET /api/stats**: Retrieve recent system usage logs.
- **GET /api/deployments**: Retrieve recent deployment history.
- **GET /api/nginx/status**: Check the status of Nginx.
- **POST /api/nginx/reload**: Reload Nginx.
- **POST /webhook/{uuid}**: Trigger a deployment for a project.

## Contributing
Feel free to fork the repository and submit pull requests. Contributions are welcome!
