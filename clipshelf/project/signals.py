"""Ensure every account has a permanent Personal collection from birth."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from clipshelf.models import Collection, User


@receiver(post_save, sender=User, dispatch_uid="clipshelf_user_personal_collection")
def ensure_personal_collection(sender, instance, created, **kwargs):
    if not created:
        return
    Collection.objects.get_or_create(
        kind=Collection.Kind.PERSONAL,
        owner=instance,
        defaults={"name": "Personal"},
    )
    if instance.default_collection_id is None:
        personal = Collection.objects.get(kind=Collection.Kind.PERSONAL, owner=instance)
        if User.objects.filter(pk=instance.pk, default_collection__isnull=True).update(
            default_collection=personal
        ):
            instance.default_collection = personal
