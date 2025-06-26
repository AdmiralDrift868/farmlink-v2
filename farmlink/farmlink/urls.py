from django.urls import path
from . import views

urlpatterns = [
    path('api/cart/', views.CartAPI.as_view()),
    path('api/order/', views.OrderAPI.as_view()),
    path('api/payment/webhook/', views.payment_webhook),
    # Add more endpoints as needed (review, shipping, analytics, etc.)
]