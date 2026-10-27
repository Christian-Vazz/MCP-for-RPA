from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from apps.http import api_login_required

from . import stats


@login_required
@ensure_csrf_cookie
def dashboard(request):
    name = (request.user.email or request.user.username).split("@")[0]
    return render(request, "dashboard/dashboard.html", {"display_name": name})


@api_login_required
@require_GET
def dashboard_data(request):
    return JsonResponse(stats.build(request.user))
