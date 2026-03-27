import os
import django
import sys

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myshopapp.settings')
sys.path.append(os.getcwd())

try:
    django.setup()
    from authentication.models import ShopifyStore
except ImportError:
    print("Error: Could not import ShopifyStore. Make sure you are in the project root.")
    sys.exit(1)

def main():
    stores = ShopifyStore.objects.filter(is_active=True)
    if not stores:
        print("No active Shopify stores found in the database.")
        return

    print(f"{'Shop URL':<30} | {'Access Token'}")
    print("-" * 70)
    for store in stores:
        print(f"{store.shop_url:<30} | {store.access_token}")

if __name__ == "__main__":
    main()
