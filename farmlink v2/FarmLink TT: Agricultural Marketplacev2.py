# farmlink_tt.py - Production-Grade Agricultural Marketplace
import re
import json
import logging
import stripe 
import requests
from decimal import Decimal
from datetime import timedelta
from django.db import models, transaction
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.hashers import make_password
from django.contrib.auth import authenticate, login
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage
from django.db.models import Q, F, CheckConstraint, Sum
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.core.cache import cache
from django.urls import path
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.mail import send_mail
from django.template.loader import render_to_string
from geopy.distance import geodesic
from rest_framework.views import APIView
from django.contrib.postgres.search import SearchVector

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ======================
# SECURE MODEL DEFINITIONS (UPDATED)
# ======================
class CARICOMCountry(models.Model):
    code = models.CharField(max_length=2, primary_key=True, editable=False)
    name = models.CharField(max_length=50, unique=True)
    currency_code = models.CharField(max_length=3, default='TTD')
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    
    class Meta:
        verbose_name_plural = "CARICOM Countries"
        ordering = ['name']

class ProductCategory(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=50)
    export_restricted = models.BooleanField(default=False)
    
    class Meta:
        verbose_name_plural = "Product Categories"

class Farmer(AbstractUser):
    # Custom fields
    farm_name = models.CharField(max_length=100, unique=True)
    country = models.ForeignKey(CARICOMCountry, on_delete=models.PROTECT)
    region = models.CharField(max_length=50)
    certification = models.CharField(max_length=100, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('verified', 'Verified'),
            ('rejected', 'Rejected')
        ],
        default='pending'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)
    location = models.CharField(max_length=100, blank=True)  # 'latitude,longitude'
    payment_method = models.CharField(max_length=50, blank=True)  # Stripe, PayPal ID
    
    # Required for custom user model
    USERNAME_FIELD = 'farm_name'
    REQUIRED_FIELDS = ['email', 'country']
    
    class Meta:
        indexes = [
            models.Index(fields=['verification_status']),
            models.Index(fields=['country', 'region']),
        ]

class Product(models.Model):
    UNIT_CHOICES = [
        ('kg', 'Kilogram'),
        ('lb', 'Pound'),
        ('crt', 'Crate'),
        ('bnd', 'Bundle'),
        ('dz', 'Dozen'),
    ]
    
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=8, 
        decimal_places=2,
        validators=[MinValueValidator(0.01)]
    )
    unit = models.CharField(max_length=5, choices=UNIT_CHOICES)
    quantity = models.PositiveIntegerField()
    category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT)
    farmer = models.ForeignKey(Farmer, on_delete=models.CASCADE, related_name='products')
    harvest_date = models.DateField()
    is_organic = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    image = models.ImageField(
        upload_to='products/',
        null=True,
        blank=True,
        storage=FileSystemStorage(location='protected_media/')
    )
    
    class Meta:
        indexes = [
            models.Index(fields=['category', 'harvest_date']),
            models.Index(fields=['price', 'quantity']),
            models.Index(fields=['name', 'description']),
        ]
        constraints = [
            CheckConstraint(
                check=Q(quantity__gte=0),
                name='non_negative_quantity'
            )
        ]

class Cart(models.Model):
    user = models.ForeignKey(Farmer, on_delete=models.CASCADE, related_name='carts')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    def total(self):
        return self.items.aggregate(total=Sum(F('quantity') * F('product__price')))['total'] or 0

class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)

class Order(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]
    
    buyer = models.ForeignKey(Farmer, on_delete=models.PROTECT, related_name='orders')
    farmer = models.ForeignKey(Farmer, on_delete=models.PROTECT, related_name='farmer_orders')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    payment_intent_id = models.CharField(max_length=100, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)
    shipping_address = models.TextField()

class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=8, decimal_places=2)

class Review(models.Model):
    RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]
    
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='review')
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class Notification(models.Model):
    TYPE_CHOICES = [
        ('order', 'Order Update'),
        ('payment', 'Payment'),
        ('system', 'System'),
        ('message', 'Message'),
    ]
    
    user = models.ForeignKey(Farmer, on_delete=models.CASCADE)
    message = models.TextField()
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    related_object_id = models.PositiveIntegerField(null=True, blank=True)

# ======================
# UTILITIES & SERVICES (UPDATED)
# ======================
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
        except:
            return False

class GeoService:
    @staticmethod
    def calculate_shipping_cost(origin, destination):
        try:
            # Get coordinates from location strings
            origin_coords = tuple(map(float, origin.split(',')))
            dest_coords = tuple(map(float, destination.split(',')))
            
            # Calculate distance in kilometers
            distance = geodesic(origin_coords, dest_coords).kilometers
            
            # Base cost + per km rate (TTD)
            base_cost = Decimal('50.00')
            per_km = Decimal('1.20')
            return base_cost + (per_km * Decimal(str(distance)))
        except:
            return Decimal('100.00')  # Default shipping cost

class PaymentService:
    def __init__(self):
        stripe.api_key = settings.STRIPE_SECRET_KEY
    
    def create_payment_intent(self, amount, currency, metadata=None):
        try:
            return stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Convert to cents
                currency=currency.lower(),
                metadata=metadata or {},
                automatic_payment_methods={"enabled": True},
            )
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error: {str(e)}")
            return None
    
    def confirm_payment(self, payment_intent_id):
        try:
            return stripe.PaymentIntent.confirm(payment_intent_id)
        except stripe.error.StripeError as e:
            logger.error(f"Payment confirmation error: {str(e)}")
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
        
        # Also send email for critical notifications
        if notif_type in ['order', 'payment']:
            subject = f"FarmLink Notification: {notif_type.capitalize()}"
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=True,
            )

class SearchService:
    @staticmethod
    def full_text_search(query):
        # Use PostgreSQL full-text search if available
        if settings.USE_POSTGRES:
            return Product.objects.annotate(
                search=SearchVector('name', 'description', 'farmer__farm_name')
            ).filter(search=query)
        
        # Fallback to basic search
        return Product.objects.filter(
            Q(name__icontains=query) |
            Q(description__icontains=query) |
            Q(farmer__farm_name__icontains=query)
        )

# ======================
# API VIEWS (UPDATED WITH NEW FUNCTIONALITY)
# ======================
class CartAPI(APIView):
    @login_required
    def get(self, request):
        cart, _ = Cart.objects.get_or_create(user=request.user, is_active=True)
        items = cart.items.select_related('product').all()
        
        cart_data = {
            'id': cart.id,
            'total': cart.total(),
            'items': [{
                'id': item.id,
                'product_id': item.product.id,
                'name': item.product.name,
                'price': str(item.product.price),
                'quantity': item.quantity,
                'subtotal': str(item.product.price * item.quantity)
            } for item in items]
        }
        return JsonResponse(cart_data)
    
    @login_required
    def post(self, request):
        data = json.loads(request.body)
        product_id = data.get('product_id')
        quantity = data.get('quantity', 1)
        
        try:
            product = Product.objects.get(id=product_id, quantity__gte=quantity)
            cart, _ = Cart.objects.get_or_create(user=request.user, is_active=True)
            
            # Update or create cart item
            cart_item, created = CartItem.objects.get_or_create(
                cart=cart,
                product=product,
                defaults={'quantity': quantity}
            )
            
            if not created:
                cart_item.quantity = F('quantity') + quantity
                cart_item.save()
            
            return JsonResponse({'status': 'success'}, status=201)
        except Product.DoesNotExist:
            return JsonResponse({'error': 'Product not available'}, status=404)

class OrderAPI(APIView):
    @login_required
    def post(self, request):
        data = json.loads(request.body)
        shipping_address = data.get('shipping_address')
        
        if not shipping_address:
            return JsonResponse({'error': 'Shipping address required'}, status=400)
        
        try:
            cart = Cart.objects.get(user=request.user, is_active=True)
            items = cart.items.select_related('product').all()
            
            if not items:
                return JsonResponse({'error': 'Cart is empty'}, status=400)
            
            with transaction.atomic():
                # Calculate total and tax
                subtotal = sum(item.product.price * item.quantity for item in items)
                tax_rate = request.user.country.tax_rate / 100
                tax_amount = subtotal * Decimal(tax_rate)
                
                # Calculate shipping cost
                if request.user.location and request.user.location != "":
                    farmer_location = items[0].product.farmer.location
                    shipping_cost = GeoService.calculate_shipping_cost(
                        farmer_location, 
                        request.user.location
                    )
                else:
                    shipping_cost = Decimal('100.00')  # Default shipping cost
                
                total_amount = subtotal + tax_amount + shipping_cost
                
                # Create order
                order = Order.objects.create(
                    buyer=request.user,
                    farmer=items[0].product.farmer,
                    total_amount=total_amount,
                    tax_amount=tax_amount,
                    shipping_cost=shipping_cost,
                    shipping_address=shipping_address
                )
                
                # Create order items and update inventory
                for item in items:
                    OrderItem.objects.create(
                        order=order,
                        product=item.product,
                        quantity=item.quantity,
                        price=item.product.price
                    )
                    
                    # Update product quantity
                    item.product.quantity = F('quantity') - item.quantity
                    item.product.save()
                
                # Create payment intent
                payment_service = PaymentService()
                payment_intent = payment_service.create_payment_intent(
                    amount=total_amount,
                    currency=request.user.country.currency_code,
                    metadata={'order_id': order.id}
                )
                
                if not payment_intent:
                    return JsonResponse({'error': 'Payment processing failed'}, status=500)
                
                order.payment_intent_id = payment_intent.id
                order.save()
                
                # Deactivate cart
                cart.is_active = False
                cart.save()
                
                # Send notifications
                NotificationService.send_notification(
                    request.user,
                    f"Order #{order.id} created. Total: {total_amount}",
                    'order',
                    order.id
                )
                
                NotificationService.send_notification(
                    order.farmer,
                    f"New order #{order.id} from {request.user.farm_name}",
                    'order',
                    order.id
                )
                
                return JsonResponse({
                    'status': 'success',
                    'order_id': order.id,
                    'client_secret': payment_intent.client_secret
                }, status=201)
        
        except Cart.DoesNotExist:
            return JsonResponse({'error': 'Active cart not found'}, status=404)
        except Exception as e:
            logger.error(f"Order creation error: {str(e)}")
            return JsonResponse({'error': 'Order processing failed'}, status=500)

@csrf_exempt
@require_http_methods(["POST"])
def payment_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)
    
    # Handle payment success
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        order = Order.objects.get(payment_intent_id=payment_intent['id'])
        order.status = 'paid'
        order.save()
        
        # Send notifications
        NotificationService.send_notification(
            order.buyer,
            f"Payment confirmed for order #{order.id}",
            'payment',
            order.id
        )
        
        NotificationService.send_notification(
            order.farmer,
            f"Payment received for order #{order.id}",
            'payment',
            order.id
        )
    
    return HttpResponse(status=200)

class ReviewAPI(APIView):
    @login_required
    def post(self, request, order_id):
        data = json.loads(request.body)
        rating = data.get('rating')
        comment = data.get('comment', '')
        
        try:
            order = Order.objects.get(id=order_id, buyer=request.user, status='delivered')
            
            # Create or update review
            review, created = Review.objects.update_or_create(
                order=order,
                defaults={
                    'rating': rating,
                    'comment': comment
                }
            )
            
            # Update farmer rating (simplified)
            farmer = order.farmer
            farmer_reviews = Review.objects.filter(order__farmer=farmer)
            avg_rating = farmer_reviews.aggregate(models.Avg('rating'))['rating__avg']
            farmer.profile.rating = avg_rating
            farmer.save()
            
            return JsonResponse({'status': 'success'}, status=201)
        except Order.DoesNotExist:
            return JsonResponse({'error': 'Order not found or not eligible for review'}, status=404)

# ======================
# SHIPPING INTEGRATION
# ======================
class ShippingAPI(APIView):
    @login_required
    def post(self, request, order_id):
        data = json.loads(request.body)
        tracking_number = data.get('tracking_number')
        
        try:
            order = Order.objects.get(id=order_id, farmer=request.user, status='paid')
            order.tracking_number = tracking_number
            order.status = 'shipped'
            order.save()
            
            # Send notification to buyer
            NotificationService.send_notification(
                order.buyer,
                f"Order #{order_id} shipped. Tracking: {tracking_number}",
                'order',
                order_id
            )
            
            return JsonResponse({'status': 'success'})
        except Order.DoesNotExist:
            return JsonResponse({'error': 'Order not found'}, status=404)

# ======================
# REPORTING & ANALYTICS
# ======================
class FarmerAnalyticsAPI(APIView):
    @login_required
    def get(self, request):
        if not hasattr(request.user, 'farmer_profile'):
            return JsonResponse({'error': 'Farmer access only'}, status=403)
        
        # Sales summary
        orders = Order.objects.filter(farmer=request.user)
        total_sales = orders.aggregate(total=Sum('total_amount'))['total'] or 0
        
        # Recent orders
        recent_orders = orders.order_by('-created_at')[:5].values(
            'id', 'created_at', 'total_amount', 'status'
        )
        
        # Top products
        top_products = OrderItem.objects.filter(
            order__farmer=request.user
        ).values('product__name').annotate(
            total_quantity=Sum('quantity'),
            total_revenue=Sum(F('quantity') * F('price'))
        ).order_by('-total_revenue')[:5]
        
        return JsonResponse({
            'total_sales': total_sales,
            'recent_orders': list(recent_orders),
            'top_products': list(top_products)
        })

# ======================
# SYSTEM INITIALIZATION (UPDATED)
# ======================
def initialize_system():
    # CARICOM Countries with currencies and tax rates
    countries = [
        {'code': 'TT', 'name': 'Trinidad and Tobago', 'currency_code': 'TTD', 'tax_rate': 12.5},
        {'code': 'JM', 'name': 'Jamaica', 'currency_code': 'JMD', 'tax_rate': 15.0},
        {'code': 'BB', 'name': 'Barbados', 'currency_code': 'BBD', 'tax_rate': 17.5},
        # Add other CARICOM members
    ]
    
    for country in countries:
        CARICOMCountry.objects.update_or_create(
            code=country['code'],
            defaults={
                'name': country['name'],
                'currency_code': country['currency_code'],
                'tax_rate': country['tax_rate']
            }
        )
    
    # Product Categories
    categories = [
        {'code': 'FRT', 'name': 'Fruits', 'export_restricted': False},
        {'code': 'VEG', 'name': 'Vegetables', 'export_restricted': False},
        # ... other categories
    ]
    
    for category in categories:
        ProductCategory.objects.update_or_create(
            code=category['code'],
            defaults={
                'name': category['name'],
                'export_restricted': category['export_restricted']
            }
        )
    
    # Configure Stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY

# ======================
# PRODUCTION CONFIGURATION (UPDATED)
# ======================
if __name__ == "__main__":
    settings.configure(
        # ... [previous settings] ...
        
        # Payment configuration
        STRIPE_SECRET_KEY='your_stripe_secret_key',
        STRIPE_WEBHOOK_SECRET='your_webhook_secret',
        DEFAULT_FROM_EMAIL='noreply@farmlink.tt',
        
        # Shipping configuration
        SHIPPING_BASE_COST=50.00,
        SHIPPING_PER_KM=1.20,
        
        # Email configuration
        EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
        EMAIL_HOST='smtp.yourdomain.com',
        EMAIL_PORT=587,
        EMAIL_USE_TLS=True,
        EMAIL_HOST_USER='your_email@domain.com',
        EMAIL_HOST_PASSWORD='your_email_password',
        
        # Search configuration
        USE_POSTGRES=True,  # Enable PostgreSQL full-text search
    )
    
    # Initialize database and system
    from django.core.management import execute_from_command_line
    execute_from_command_line(['manage.py', 'migrate'])
    initialize_system()
    
    # Start production server
    from django.core.management.commands.runserver import Command as Runserver
    Runserver().run(addrport='0.0.0.0:8000', use_reloader=False)