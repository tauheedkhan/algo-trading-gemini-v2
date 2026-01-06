import asyncio
import os
import ccxt.async_support as ccxt
from dotenv import load_dotenv

async def test_connection():
    load_dotenv()
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    
    print(f"Testing with Key: {api_key[:4]}...{api_key[-4:]}")
    
    # Configuration exactly as in the bot
    config = {
        'apiKey': api_key,
        'secret': api_secret,
        'verbose': True, # See the exact URL
        'options': {
            'defaultType': 'future',
            'sandboxMode': True,
        },
        'urls': {
            'api': {
                'public': 'https://testnet.binancefuture.com/fapi/v1',
                'private': 'https://testnet.binancefuture.com/fapi/v1',
                'fapiPublic': 'https://testnet.binancefuture.com/fapi/v1',
                'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
                'sapi': 'https://testnet.binancefuture.com/fapi/v1',
            }
        },
        'has': {
            'fetchCurrencies': False
        }
    }
    
    exchange = ccxt.binance(config)
    
    try:
        print("1. Loading Markets...")
        await exchange.load_markets()
        print("   [OK] Markets Loaded")
        
        print("2. Fetching Balance (Private API Check)...")
        balance = await exchange.fetch_balance()
        print("   [OK] Balance Fetched!")
        print(f"   USDT Free: {balance.get('USDT', {}).get('free', 0)}")
        
    except Exception as e:
        print(f"\n[ERROR] Connection Failed: {e}")
        print("\nPossible Causes:")
        print("- Invalid API Key/Secret")
        print("- Keys are for Spot Testnet, not Futures Testnet")
        print("- Keys are for Mainnet, not Testnet")
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(test_connection())
