import hashlib
import hmac
import time
import requests
import os
from dotenv import load_dotenv

def verify_raw():
    load_dotenv()
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    
    if not api_key or not api_secret:
        print("Error: Missing Keys")
        return

    base_url = "https://testnet.binancefuture.com"
    endpoint = "/fapi/v2/account"
    
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    
    signature = hmac.new(
        api_secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}
    
    print(f"Testing Raw Request: {url}")
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            print("\n[SUCCESS] Connection Established!")
            print(f"Can Trade: {data.get('canTrade')}")
            print(f"Assets: {len(data.get('assets', []))}")
            # Show USDT balance
            for asset in data.get('assets', []):
                if asset['asset'] == 'USDT':
                    print(f"USDT Balance: {asset.get('walletBalance')}")
        else:
            print(f"\n[FAILED] Status: {response.status_code}")
            print(f"Response: {response.text}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    verify_raw()
