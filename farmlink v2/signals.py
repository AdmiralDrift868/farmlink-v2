# marketplace/signals.py
from django.db.models.signals import post_migrate
from .models import initialize_system

def init_system(sender, **kwargs):
    initialize_system()

post_migrate.connect(init_sender, sender=MarketplaceConfig)