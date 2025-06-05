# marketplace/middleware.py
from django.http import HttpResponseForbidden

class MediaAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/protected_media/'):
            if not request.user.is_authenticated:
                return HttpResponseForbidden()
        return self.get_response(request)