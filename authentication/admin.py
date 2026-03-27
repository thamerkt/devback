from django.contrib import admin
from .models import ShopifyStore


@admin.register(ShopifyStore)
class ShopifyStoreAdmin(admin.ModelAdmin):
    list_display = ('shop_url', 'is_active', 'scope', 'installed_at', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('shop_url',)
    readonly_fields = ('installed_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('shop_url', 'is_active')
        }),
        ('OAuth', {
            'fields': ('access_token', 'scope', 'nonce'),
        }),
        ('Timestamps', {
            'fields': ('installed_at', 'updated_at'),
        }),
    )
