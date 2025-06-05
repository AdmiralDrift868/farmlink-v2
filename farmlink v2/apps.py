# marketplace/apps.py (for data initialization)
from django.apps import AppConfig

class MarketplaceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'marketplace'

    def ready(self):
        from .models import initialize_system
        initialize_system()