from django.conf import settings
from django.contrib.auth import login, logout
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from apps.http import api_login_required, error, json_body

from .forms import EmailLoginForm, RegisterForm
from .models import Profile, get_profile

LOCK_SECONDS = 15 * 60


def _fail_key(request, email):
    return f"loginfail:{request.META.get('REMOTE_ADDR', '?')}:{email}"


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("chat")
    form = EmailLoginForm(request, data=request.POST or None)
    locked = False
    if request.method == "POST":
        email = (request.POST.get("username") or "").strip().lower()
        key = _fail_key(request, email)
        if cache.get(key, 0) >= settings.ORBIT["LOGIN_MAX_ATTEMPTS"]:
            locked = True
        elif form.is_valid():
            login(request, form.get_user())
            cache.delete(key)
            nxt = request.GET.get("next", "")
            if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)
            return redirect("chat")
        else:
            cache.set(key, cache.get(key, 0) + 1, LOCK_SECONDS)
    return render(request, "accounts/login.html", {"form": form, "locked": locked})


@ensure_csrf_cookie
def register_view(request):
    if request.user.is_authenticated:
        return redirect("chat")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("chat")
    return render(request, "accounts/register.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


@api_login_required
@require_POST
def profile_update(request):
    body = json_body(request)
    profile = get_profile(request.user)
    changed = []
    if "theme" in body:
        if body["theme"] not in dict(Profile.THEMES):
            return error("Tema inválido.")
        profile.theme = body["theme"]
        changed.append("theme")
    for flag in ("auto_approve", "allow_learning"):
        if flag in body:
            setattr(profile, flag, bool(body[flag]))
            changed.append(flag)
    if changed:
        profile.save(update_fields=changed)
        cache.delete(f"dash:{request.user.pk}")
    return JsonResponse({"theme": profile.theme, "auto_approve": profile.auto_approve,
                         "allow_learning": profile.allow_learning})
