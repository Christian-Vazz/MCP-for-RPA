from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

from .models import Profile

User = get_user_model()


class EmailLoginForm(AuthenticationForm):
    username = forms.EmailField(
        label="E-mail", max_length=150,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )
    password = forms.CharField(
        label="Senha", strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    error_messages = {
        "invalid_login": "E-mail ou senha incorretos.",
        "inactive": "Esta conta está inativa.",
    }

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()


class RegisterForm(forms.Form):
    email = forms.EmailField(label="E-mail", max_length=150,
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}))
    password1 = forms.CharField(label="Senha", strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Confirmar senha", strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    allow_learning = forms.BooleanField(
        required=False,
        label="Autorizo o armazenamento de amostras das minhas solicitações e dos sites e arquivos usados, "
              "para melhorar o agente (você pode mudar isso depois).",
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("Este e-mail já está em uso.")
        return email

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "As senhas não coincidem.")
        elif p1:
            try:
                validate_password(p1)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data

    def save(self):
        email = self.cleaned_data["email"]
        user = User.objects.create_user(username=email, email=email, password=self.cleaned_data["password1"])
        Profile.objects.create(user=user, allow_learning=self.cleaned_data["allow_learning"])
        return user
