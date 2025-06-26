from decimal import Decimal
from django.db import transaction
from django.db.models import F
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authentication import SessionAuthentication, BasicAuthentication, TokenAuthentication
from .models import Cart, CartItem, Order, OrderItem, Product
from .serializers import CartSerializer
from .permissions import IsFarmer, IsBuyer
from .pagination import StandardResultsSetPagination
from .services import PaymentService, NotificationService, GeoService, log_audit
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.http import HttpResponse
import os
import stripe

class CartAPI(APIView):
    authentication_classes = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
    permission_classes = [IsBuyer]

    def get(self, request):
        cart, _ = Cart.objects.get_or_create(user=request.user, is_active=True)
        serializer = CartSerializer(cart)
        log_audit('cart_view', request.user, 'Cart', cart.id)
        return Response(serializer.data)

    def post(self, request):
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
    permission_classes = [IsBuyer]

    def post(self, request):
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
            return Response({'error': 'Order processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@require_http_methods(["POST"])
def payment_webhook(request):
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