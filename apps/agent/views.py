from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie

from apps.accounts.models import get_profile


@login_required
@ensure_csrf_cookie
def chat(request):
    profile = get_profile(request.user)
    name = (request.user.email or request.user.username).split("@")[0]
    return render(request, "agent/chat.html", {"profile": profile, "display_name": name})
