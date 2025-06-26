import os
import re
import json
import logging
from decimal import Decimal
from typing import Tuple, Optional

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.mail import send_mail
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q, F, Sum, Avg, CheckConstraint
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.urls import path
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth.models import AbstractUser, Group
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.authentication import SessionAuthentication, BasicAuthentication, TokenAuthentication
from rest_framework.response import Response
from rest_framework import status, serializers, pagination, throttling, exceptions, schemas
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from drf_spectacular.utils import extend_schema, OpenApiParameter
from geopy.distance import geodesic

import stripe

# ========== LOGGING ==========
logger = logging.getLogger("farmlink")
logging.basicConfig(level=logging.INFO)

# ========== MODELS ==========

class CARICOMCountry(models.Model):
    code = models.CharField(max_length=2, primary_key=True, editable=False)
    name = models.CharField(max_length=50, unique=True)
    currency_code = models.CharField(max_length=3, default='TTD')
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)

    class Meta:
        verbose_name_plural = "CARICOM Countries"
        ordering = ['name']

    def __str__(self):
        return self.name

class ProductCategory(models.Model):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=50)
    export_restricted = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "Product Categories"
    def __str__(self):
        return self.name

class Farmer(AbstractUser):
    ROLE_CHOICES = [
        ('farmer', 'Farmer'),
        ('buyer', 'Buyer'),
        ('admin', 'Admin'),
    ]
    farm_name = models.CharField(max_length=100, unique=True)
    country = models.ForeignKey(CARICOMCountry, on_delete=models.PROTECT)
    region = models.CharField(max_length=50)
    certification = models.CharField(max_length=100, blank=True)
    verification_status = models.CharField(
        max_length=20,
        choices=[('pending', 'Pending'), ('verified', 'Verified'), ('rejected', 'Rejected')],
        default='pending'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)
    location = models.CharField(max_length=100, blank=True)  # 'lat,lon'
    payment_method = models.CharField(max_length=50, blank=True)
    # NEW: Add a role field for permissions
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='buyer')

    USERNAME_FIELD = 'farm_name'
    REQUIRED_FIELDS = ['email', 'country']

    class Meta:
        indexes = [
            models.Index(fields=['verification_status']),
            models.Index(fields=['country', 'region']),
        ]
    def __str__(self):
        return self.farm_name

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
    price = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)])
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
            CheckConstraint(check=Q(quantity__gte=0), name='non_negative_quantity')
        ]
    def __str__(self):
        return self.name

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
    # NEW: Add audit fields
    last_action_by = models.ForeignKey(Farmer, null=True, blank=True, on_delete=models.SET_NULL, related_name='order_actions')

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

class AuditLog(models.Model):
    action = models.CharField(max_length=100)
    user = models.ForeignKey(Farmer, null=True, on_delete=models.SET_NULL)
    timestamp = models.DateTimeField(auto_now_add=True)
    object_type = models.CharField(max_length=50)
    object_id = models.PositiveIntegerField()
    details = models.JSONField(default=dict)

# ========== SERIALIZERS ==========

class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = '__all__'

class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    class Meta:
        model = CartItem
        fields = ['id', 'product', 'quantity', 'added_at']

class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True)
    class Meta:
        model = Cart
        fields = ['id', 'user', 'created_at', 'is_active', 'items']

class OrderItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer()
    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'quantity', 'price']

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True)
    class Meta:
        model = Order
        fields = ['id', 'buyer', 'farmer', 'created_at', 'status', 'total_amount', 'items']

class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = '__all__'

# ========== PAGINATION ==========

class StandardResultsSetPagination(pagination.PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100

# ========== PERMISSIONS ==========

class IsFarmer(BasePermission):
    def has_permission(self, request, view):
        return hasattr(request.user, 'role') and request.user.role == 'farmer'

class IsBuyer(BasePermission):
    def has_permission(self, request, view):
        return hasattr(request.user, 'role') and request.user.role == 'buyer'

class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return hasattr(request.user, 'role') and request.user.role == 'admin'

# ========== THROTTLING ==========

class BurstRateThrottle(throttling.UserRateThrottle):
    rate = '20/min'

class SustainedRateThrottle(throttling.UserRateThrottle):
    rate = '100/hour'

# ========== UTILITIES & SERVICES ==========

class DataValidator:
    @staticmethod
    def validate_password(password: str) -> Tuple[bool, str]:
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
    def validate_email(email: str) -> bool:
        pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
        return bool(re.match(pattern, email))

    @staticmethod
    def sanitize_input(text: str) -> str:
        return re.sub(r'[<>"\']', '', text).strip() if text else text

    @staticmethod
    def validate_location(location: str) -> bool:
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
    def calculate_shipping_cost(origin: str, destination: str) -> Decimal:
        try:
            origin_coords = tuple(map(float, origin.split(',')))
            dest_coords = tuple(map(float, destination.split(',')))
            distance = geodesic(origin_coords, dest_coords).kilometers
            base_cost = Decimal(str(getattr(settings, "SHIPPING_BASE_COST", 50.0)))
            per_km = Decimal(str(getattr(settings, "SHIPPING_PER_KM", 1.20)))
            return base_cost + (per_km * Decimal(str(distance)))
        except Exception as e:
            logger.error(f"Shipping cost calculation failed: {e}")
            return Decimal('100.00')

class PaymentService:
    def __init__(self):
        stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')

    def create_payment_intent(self, amount: Decimal, currency: str, metadata: dict = None):
        try:
            return stripe.PaymentIntent.create(
                amount=int(amount * 100),  # cents
                currency=currency.lower(),
                metadata=metadata or {},
                automatic_payment_methods={"enabled": True}
            )
        except stripe.error.StripeError as e:
            logger.error(f"Stripe error: {e}")
            return None

    def confirm_payment(self, payment_intent_id: str):
        try:
            return stripe.PaymentIntent.confirm(payment_intent_id)
        except stripe.error.StripeError as e:
            logger.error(f"Payment confirmation error: {e}")
            return None

class NotificationService:
    @staticmethod
    def send_notification(user: Farmer, message: str, notif_type: str, related_id: Optional[int] = None):
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

# ========== API VIEWS ==========

class CartAPI(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated, IsBuyer]
    throttle_classes = [BurstRateThrottle, SustainedRateThrottle]

    @extend_schema(
        responses=CartSerializer,
        parameters=[OpenApiParameter(name='page', type=int, location=OpenApiParameter.QUERY)]
    )
    def get(self, request: HttpRequest):
        cart, _ = Cart.objects.get_or_create(user=request.user, is_active=True)
        serializer = CartSerializer(cart)
        log_audit('cart_view', request.user, 'Cart', cart.id)
        return Response(serializer.data)

    def post(self, request: HttpRequest):
        data = request.data
        product_id = data.get('product_id')
        quantity = int(data.get('quantity', 1))
        if not product_id or quantity < 1:
            return Response({'error': 'Invalid product ID or quantity.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            product = Product.objects.get(id=product_id, quantity__gte=quantity)
            cart, _ = Cart.objects.get_or_create(user=request.user, is_active=True)
            cart_item, created = CartItem.objects.get_or_create(
                cart=cart,
                product=product,
                defaults={'quantity': quantity}
            )
            if not created:
                cart_item.quantity = F('quantity') + quantity
                cart_item.save()
            log_audit('cart_add_item', request.user, 'CartItem', cart_item.id)
            return Response({'status': 'success'}, status=status.HTTP_201_CREATED)
        except Product.DoesNotExist:
            return Response({'error': 'Product not available'}, status=status.HTTP_404_NOT_FOUND)

class OrderAPI(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated, IsBuyer]
    throttle_classes = [BurstRateThrottle, SustainedRateThrottle]

    def post(self, request: HttpRequest):
        data = request.data
        shipping_address = data.get('shipping_address')
        if not shipping_address:
            return Response({'error': 'Shipping address required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            cart = Cart.objects.get(user=request.user, is_active=True)
            items = cart.items.select_related('product').all()
            if not items:
                return Response({'error': 'Cart is empty'}, status=status.HTTP_400_BAD_REQUEST)
            with transaction.atomic():
                subtotal = sum(item.product.price * item.quantity for item in items)
                tax_rate = request.user.country.tax_rate / 100
                tax_amount = subtotal * Decimal(tax_rate)
                farmer_location = items[0].product.farmer.location
                shipping_cost = GeoService.calculate_shipping_cost(
                    farmer_location,
                    request.user.location or ""
                )
                total_amount = subtotal + tax_amount + shipping_cost
                order = Order.objects.create(
                    buyer=request.user,
                    farmer=items[0].product.farmer,
                    total_amount=total_amount,
                    tax_amount=tax_amount,
                    shipping_cost=shipping_cost,
                    shipping_address=shipping_address,
                    last_action_by=request.user
                )
                for item in items:
                    OrderItem.objects.create(
                        order=order,
                        product=item.product,
                        quantity=item.quantity,
                        price=item.product.price
                    )
                    item.product.quantity = F('quantity') - item.quantity
                    item.product.save()
                payment_service = PaymentService()
                payment_intent = payment_service.create_payment_intent(
                    amount=total_amount,
                    currency=request.user.country.currency_code,
                    metadata={'order_id': order.id}
                )
                if not payment_intent:
                    return Response({'error': 'Payment processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                order.payment_intent_id = payment_intent.id
                order.save()
                cart.is_active = False
                cart.save()
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
                log_audit('order_create', request.user, 'Order', order.id, {'amount': str(total_amount)})
                return Response({
                    'status': 'success',
                    'order_id': order.id,
                    'client_secret': payment_intent.client_secret
                }, status=status.HTTP_201_CREATED)
        except Cart.DoesNotExist:
            return Response({'error': 'Active cart not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Order creation error: {e}")
            return Response({'error': 'Order processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt  # CSRF exempt for webhooks, but validate signature!
@require_http_methods(["POST"])
def payment_webhook(request: HttpRequest):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.environ.get('STRIPE_WEBHOOK_SECRET', '')
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        try:
            order = Order.objects.get(payment_intent_id=payment_intent['id'])
        except Order.DoesNotExist:
            return HttpResponse(status=404)
        order.status = 'paid'
        order.save()
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
        log_audit('order_paid', order.buyer, 'Order', order.id)
    return HttpResponse(status=200)

class ReviewAPI(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated, IsBuyer]
    throttle_classes = [BurstRateThrottle]

    def post(self, request: HttpRequest, order_id: int):
        data = request.data
        rating = data.get('rating')
        comment = data.get('comment', '')
        if not rating or int(rating) not in range(1, 6):
            return Response({'error': 'Rating must be between 1 and 5.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            order = Order.objects.get(id=order_id, buyer=request.user, status='delivered')
            review, created = Review.objects.update_or_create(
                order=order,
                defaults={'rating': rating, 'comment': comment}
            )
            farmer = order.farmer
            farmer_reviews = Review.objects.filter(order__farmer=farmer)
            avg_rating = farmer_reviews.aggregate(Avg('rating'))['rating__avg']
            # Add: Save to farmer profile if exists
            if hasattr(farmer, 'profile'):
                farmer.profile.rating = avg_rating
                farmer.profile.save()
            log_audit('review_create', request.user, 'Review', review.id)
            return Response({'status': 'success'}, status=status.HTTP_201_CREATED)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found or not eligible for review'}, status=status.HTTP_404_NOT_FOUND)

class ShippingAPI(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated, IsFarmer]
    throttle_classes = [BurstRateThrottle]

    def post(self, request: HttpRequest, order_id: int):
        data = request.data
        tracking_number = data.get('tracking_number')
        if not tracking_number:
            return Response({'error': 'Tracking number required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            order = Order.objects.get(id=order_id, farmer=request.user, status='paid')
            order.tracking_number = tracking_number
            order.status = 'shipped'
            order.last_action_by = request.user
            order.save()
            NotificationService.send_notification(
                order.buyer,
                f"Order #{order_id} shipped. Tracking: {tracking_number}",
                'order',
                order_id
            )
            log_audit('order_shipped', request.user, 'Order', order.id, {'tracking_number': tracking_number})
            return Response({'status': 'success'})
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

class FarmerAnalyticsAPI(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated, IsFarmer]
    throttle_classes = [SustainedRateThrottle]

    def get(self, request: HttpRequest):
        orders = Order.objects.filter(farmer=request.user)
        paginator = StandardResultsSetPagination()
        paginated_orders = paginator.paginate_queryset(orders.order_by('-created_at'), request)
        total_sales = orders.aggregate(total=Sum('total_amount'))['total'] or 0
        top_products = OrderItem.objects.filter(
            order__farmer=request.user
        ).values('product__name').annotate(
            total_quantity=Sum('quantity'),
            total_revenue=Sum(F('quantity') * F('price'))
        ).order_by('-total_revenue')[:5]
        log_audit('analytics_view', request.user, 'Order', 0)
        return paginator.get_paginated_response({
            'total_sales': total_sales,
            'recent_orders': [
                {'id': o.id, 'created_at': o.created_at, 'total_amount': o.total_amount, 'status': o.status}
                for o in paginated_orders
            ],
            'top_products': list(top_products)
        })

# ========== SYSTEM INITIALIZATION ==========

def initialize_system():
    countries = [
        {'code': 'TT', 'name': 'Trinidad and Tobago', 'currency_code': 'TTD', 'tax_rate': 12.5},
        {'code': 'JM', 'name': 'Jamaica', 'currency_code': 'JMD', 'tax_rate': 15.0},
        {'code': 'BB', 'name': 'Barbados', 'currency_code': 'BBD', 'tax_rate': 17.5},
    ]
    for country in countries:
        CARICOMCountry.objects.update_or_create(
            code=country['code'],
            defaults={
                'name': country['name'],
                'currency_code': country['currency_code'],
                'tax_rate': country['tax_rate'],
            }
        )
    categories = [
        {'code': 'FRT', 'name': 'Fruits', 'export_restricted': False},
        {'code': 'VEG', 'name': 'Vegetables', 'export_restricted': False},
    ]
    for category in categories:
        ProductCategory.objects.update_or_create(
            code=category['code'],
            defaults={
                'name': category['name'],
                'export_restricted': category['export_restricted'],
            }
        )

# ========== URLS & API DOCS ==========

urlpatterns = [
    path('api/cart/', CartAPI.as_view()),
    path('api/order/', OrderAPI.as_view()),
    path('api/review/<int:order_id>/', ReviewAPI.as_view()),
    path('api/shipping/<int:order_id>/', ShippingAPI.as_view()),
    path('api/analytics/', FarmerAnalyticsAPI.as_view()),
    path('api/payment/webhook/', payment_webhook),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]
