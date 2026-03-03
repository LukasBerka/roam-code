"""Post-indexing Django inheritance and custom field resolution.

Runs after all files are parsed and edges are resolved. Queries the DB
to build a cross-file inheritance graph, then batch-updates framework_type
and field metadata for Django models and custom fields.
"""

from __future__ import annotations

import json

# Duplicated from python_lang.py to avoid circular imports.
# These are stable constants unlikely to diverge.
_DJANGO_MODEL_BASES = frozenset({
    "Model", "models.Model", "TimeStampedModel",
    "AbstractUser", "AbstractBaseUser",
})

_DJANGO_FIELD_TYPES = frozenset({
    "CharField", "IntegerField", "FloatField", "DecimalField",
    "BooleanField", "TextField", "DateField", "DateTimeField",
    "TimeField", "EmailField", "URLField", "UUIDField",
    "SlugField", "FileField", "ImageField", "JSONField",
    "BinaryField", "AutoField", "BigAutoField", "SmallAutoField",
    "BigIntegerField", "SmallIntegerField", "PositiveIntegerField",
    "PositiveSmallIntegerField", "PositiveBigIntegerField",
    "DurationField", "GenericIPAddressField", "FilePathField",
    "ForeignKey", "OneToOneField", "ManyToManyField",
})

_DJANGO_RELATIONSHIP_FIELDS = frozenset({
    "ForeignKey", "OneToOneField", "ManyToManyField",
})

_DJANGO_REL_KIND = {
    "ForeignKey": "django_fk",
    "OneToOneField": "django_o2o",
    "ManyToManyField": "django_m2m",
}
