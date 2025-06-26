import os
import re
from decimal import Decimal
from django.core.mail import send_mail
from django.conf import settings
from geopy.distance import geodesic
import stripe
from .models import Notification, AuditLog

class DataValidator:
    @staticmethod
    def validate_password(password):
        if len(password) < 8:
            return False, "Password must be at least 8 characters"
        if not re.search(r"[A-Z]", password):
            return False, "Password must contain an uppercase letter"
        if not re.search(r"[a-z]", password):
            return False, "Password must contain a lowercase letter"
        if not re.search(r"[0-9]", password):
            return False, "Password must contain a digit"
        return True, ""

    @staticmethod
    def validate_email(email):
        pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
        return bool(re.match(pattern, email))

    @staticmethod
    def sanitize_input(text):
        return re.sub(r'[<>"\']', '', text).strip() if text else text

    @staticmethod
    def validate_location(location):
        try:
            if not location:
                return False
            lat, lon = location.split(',')
            float(lat), float(lon)
            return True
        except Exception:
            return False

class GeoService:
    @staticmethod
    def calculate_shipping_cost(origin, destination):
        try:
            origin_coords = tuple(map(float, origin.split(',')))
            dest_coords = tuple(map(float, destination.split(',')))
            distance = geodesic(origin_coords, dest_coords).kilometers
            base_cost = Decimal(str(getattr(settings, "SHIPPING_BASE_COST", 50.0)))
            per_km = Decimal(str(getattr(settings, "SHIPPING_PER_KM", 1.20)))
            return base_cost + (per_km * Decimal(str(distance)))
        except Exception as e:
            return Decimal('100.00')

class PaymentService:
    def __init__(self):
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')

    def create_payment_intent(self, amount, currency, metadata=None):
        try:
            return stripe.PaymentIntent.create(
                amount=int(amount * 100),
                currency=currency.lower(),
                metadata=metadata or {},
                automatic_payment_methods={"enabled": True}
            )
        except stripe.error.StripeError:
            return None

    def confirm_payment(self, payment_intent_id):
        try:
            return stripe.PaymentIntent.confirm(payment_intent_id)
        except stripe.error.StripeError:
            return None

class NotificationService:
    @staticmethod
    def send_notification(user, message, notif_type, related_id=None):
        Notification.objects.create(
            user=user,
            message=message,
            notification_type=notif_type,
            related_object_id=related_id
        )
        if notif_type in ['order', 'payment']:
            subject = f"FarmLink Notification: {notif_type.capitalize()}"
            send_mail(
                subject,
                message,
                os.environ.get('DEFAULT_FROM_EMAIL', ''),
                [user.email],
                fail_silently=True,
            )

def log_audit(action, user, object_type, object_id, details=None):
    AuditLog.objects.create(
        action=action,
        user=user,
        object_type=object_type,
        object_id=object_id,
        details=details or {}
    )