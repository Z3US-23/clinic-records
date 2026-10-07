from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Who looked at or changed what, and when. Written via apps.core.audit.log_action()."""

    class Action(models.TextChoices):
        VIEW = "view", "Viewed"
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        DELETE = "delete", "Deleted"
        EXPORT = "export", "Exported"
        IMPORT = "import", "Imported"
        SEND = "send", "Sent message"
        LOGIN = "login", "Signed in"
        LOGOUT = "logout", "Signed out"
        LOGIN_FAILED = "login_failed", "Failed sign-in"

    clinic = models.ForeignKey(
        "accounts.Clinic", on_delete=models.CASCADE, null=True, blank=True, related_name="audit_logs"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    object_type = models.CharField(max_length=50, blank=True)
    object_id = models.CharField(max_length=50, blank=True)
    summary = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["clinic", "-created_at"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.user} {self.action} {self.object_type} {self.object_id}"


class SetupChecklist(models.Model):
    """The "Get your clinic ready" card on a clinic owner's Today page. A row exists once the owner hides it."""

    clinic = models.OneToOneField("accounts.Clinic", on_delete=models.CASCADE, related_name="setup_checklist")
    hidden_at = models.DateTimeField(null=True, blank=True, help_text="When the owner hid the checklist")
    hidden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return f"Setup checklist for {self.clinic}"
