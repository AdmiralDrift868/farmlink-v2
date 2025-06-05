# marketplace/urls.py
from django.urls import path
from .views import CartAPI, OrderAPI, ReviewAPI, ShippingAPI, FarmerAnalyticsAPI, payment_webhook

urlpatterns = [
    path('cart/', CartAPI.as_view()),
    path('order/', OrderAPI.as_view()),
    path('review/<int:order_id>/', ReviewAPI.as_view()),
    path('shipping/<int:order_id>/', ShippingAPI.as_view()),
    path('analytics/', FarmerAnalyticsAPI.as_view()),
    path('webhook/payment/', payment_webhook),
]