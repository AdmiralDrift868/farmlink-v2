import os
from decimal import Decimal
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.validators import MinValueValidator
from django.db import models
from django.contrib.auth.models import AbstractUser

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
    location = models.CharField(max_length=100, blank=True)
    payment_method = models.CharField(max_length=50, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='buyer')

    USERNAME_FIELD = 'farm_name'
    REQUIRED_FIELDS = ['email', 'country']

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

class Cart(models.Model):
    user = models.ForeignKey(Farmer, on_delete=models.CASCADE, related_name='carts')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

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