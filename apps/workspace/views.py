import mimetypes

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from apps.errors import UserFacingError
from apps.http import api_login_required, error, json_body

from . import sandbox


def _snapshot(user):
    return {"files": sandbox.list_files(user), "usage": sandbox.usage(user)}


@api_login_required
@require_GET
def files_list(request):
    return JsonResponse(_snapshot(request.user))


@api_login_required
@require_POST
def files_upload(request):
    results, errors = [], []
    for f in request.FILES.getlist("files")[:10]:
        try:
            results.append(sandbox.save_upload(request.user, f))
        except UserFacingError as exc:
            errors.append({"name": f.name, "error": str(exc)})
    return JsonResponse({"uploaded": results, "errors": errors, **_snapshot(request.user)})


@api_login_required
@require_GET
def files_read(request):
    rel = request.GET.get("path", "")
    try:
        path = sandbox.resolve(request.user, rel, must_exist=True)
        ext = path.suffix.lower()
        editable = ext in sandbox.TEXT_EXT
        if ext in sandbox.READ_EXT:
            text = sandbox.read_any(request.user, rel)
            kind = "text"
        else:
            text, kind = "", "binary"
        return JsonResponse({
            "path": sandbox.clean_rel(rel), "kind": kind, "text": text[:100_000],
            "truncated": len(text) > 100_000, "editable": editable,
            "size": path.stat().st_size, "can_undo": sandbox.can_undo(request.user, rel),
        })
    except UserFacingError as exc:
        return error(str(exc), 404)


@api_login_required
@require_GET
def files_download(request):
    try:
        path = sandbox.resolve(request.user, request.GET.get("path", ""), must_exist=True)
    except UserFacingError as exc:
        return error(str(exc), 404)
    if not path.is_file():
        return error("Arquivo inválido.", 404)
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name, content_type=ctype)


@api_login_required
@require_POST
def files_delete(request):
    try:
        out = sandbox.delete(request.user, json_body(request).get("path", ""))
    except UserFacingError as exc:
        return error(str(exc))
    return JsonResponse({**out, **_snapshot(request.user)})


@api_login_required
@require_POST
def files_undo(request):
    try:
        out = sandbox.undo(request.user, json_body(request).get("path", ""))
    except UserFacingError as exc:
        return error(str(exc))
    return JsonResponse({**out, **_snapshot(request.user)})
