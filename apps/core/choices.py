from django.db import models


class CertificateType(models.TextChoices):
    OFFER_LETTER = "offer_letter", "Offer Letter"
    EXPERIENCE = "experience", "Experience / Internship Certificate"
    PROJECT = "project", "Project Completion Certificate"
    LOR = "lor", "Letter of Recommendation"
