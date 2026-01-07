Here's how to run the bot as a systemd service on a GCP VM:

  1. SSH into your VM

  gcloud compute ssh YOUR_VM_NAME --zone=YOUR_ZONE

  2. Upload your code

  # From your local machine
  gcloud compute scp --recurse ~/projects/bots/algo-trading-gemini YOUR_VM_NAME:~ --zone=YOUR_ZONE

  3. Install dependencies on VM

  sudo apt update && sudo apt install -y python3-pip python3-venv
  cd ~/algo-trading-gemini
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt

  4. Create environment file

  sudo nano /etc/algo-trading.env

  Add your keys:
  BINANCE_API_KEY=your_api_key
  BINANCE_API_SECRET=your_api_secret
  TELEGRAM_BOT_TOKEN=your_telegram_token
  TELEGRAM_CHAT_ID=your_chat_id

  Secure it:
  sudo chmod 600 /etc/algo-trading.env

  5. Create systemd service

  sudo nano /etc/systemd/system/algo-trading.service

  [Unit]
  Description=Algo Trading Bot
  After=network.target

  [Service]
  Type=simple
  User=YOUR_USERNAME
  WorkingDirectory=/home/YOUR_USERNAME/algo-trading-gemini
  EnvironmentFile=/etc/algo-trading.env
  ExecStart=/home/YOUR_USERNAME/algo-trading-gemini/venv/bin/python main.py
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target

  6. Enable and start

  sudo systemctl daemon-reload
  sudo systemctl enable algo-trading
  sudo systemctl start algo-trading

  7. Useful commands

  | Command                                              | Description  |
  |------------------------------------------------------|--------------|
  | sudo systemctl status algo-trading                   | Check status |
  | sudo journalctl -u algo-trading -f                   | Live logs    |
  | sudo journalctl -u algo-trading --since "1 hour ago" | Recent logs  |
  | sudo systemctl restart algo-trading                  | Restart bot  |
  | sudo systemctl stop algo-trading                     | Stop bot     |

  Optional: Log rotation

  sudo nano /etc/logrotate.d/algo-trading

  /var/log/algo-trading/*.log {
      daily
      rotate 7
      compress
      missingok
      notifempty
  }