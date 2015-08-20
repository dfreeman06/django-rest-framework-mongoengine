from datetime import datetime 
import mongoengine as me 
from unittest import TestCase
from bson import objectid

from django.conf import settings

from rest_framework_mongoengine.serializers import DocumentSerializer
from test_models import Something

class TestDocumentSerializer(TestCase):

    def test_serializes_object(self):
        self.assertTrue(True)