from bson.errors import InvalidId
from django.core.exceptions import ValidationError
from django.utils.encoding import smart_str
from mongoengine import dereference
from mongoengine import ReferenceField as RefField
from mongoengine.base import get_document
from mongoengine.errors import DoesNotExist
from mongoengine.base.document import BaseDocument
from mongoengine.document import Document
from mongoengine import fields
from rest_framework import serializers
from mongoengine.fields import ObjectId
from bson import json_util
import json

import sys
from bson import DBRef
from rest_framework.reverse import reverse
from django.core.urlresolvers import resolve, get_script_prefix, NoReverseMatch
import re
from django.core import validators

if sys.version_info[0] >= 3:
    def unicode(val):
        return str(val)


class MongoDocumentField(serializers.WritableField):
    MAX_RECURSION_DEPTH = 5  # default value of depth
    HYPERLINK = False

    def __init__(self, *args, **kwargs):
        try:
            self.model_field = kwargs.pop('model_field')
            self.depth = kwargs.pop('depth', self.MAX_RECURSION_DEPTH)
        except KeyError:
            raise ValueError("%s requires 'model_field' kwarg" % self.type_label)

        super(MongoDocumentField, self).__init__(*args, **kwargs)

    def to_native(self, value):
        if value is not None:
            return json_util._json_convert(self.model_field.to_mongo(value))

    def from_native(self, value):
        if value in validators.EMPTY_VALUES:
            return self.empty
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
        return super(MongoDocumentField, self).from_native(value)

hexaPattern = re.compile(r'[0-9a-fA-F]{24}')


class ReferenceField(MongoDocumentField):
    type_label = 'ReferenceField'
    empty = None

    def from_native(self, value):
        if value in validators.EMPTY_VALUES:
            return None
        #TODO detect is value is a URI and extract appropriate objID
        # django.core.validators.URLValidator
        if len(value.split('/')) > 1:
            objIds = re.findall(hexaPattern, value)
            if len(objIds) > 0:
                return self.from_native(objIds[-1])

        try:
            dbref = self.model_field.to_python(value)
        except InvalidId:
            raise ValidationError(self.error_messages['invalid'])
        instance = self.model_field.document_type.objects.get(id=dbref.id)
        # Check if dereference was successful
        if not isinstance(instance, Document):
            msg = self.error_messages['invalid']
            raise ValidationError(msg)
        return instance


class ListField(MongoDocumentField):
    type_label = 'ListField'
    empty = []


class EmbeddedDocumentField(MongoDocumentField):
    type_label = 'EmbeddedDocumentField'

    def __init__(self, *args, **kwargs):
        try:
            self.document_type = kwargs.pop('document_type')
        except KeyError:
            raise ValueError("EmbeddedDocumentField requires 'document_type' kwarg")

        super(EmbeddedDocumentField, self).__init__(*args, **kwargs)

    def get_default_value(self):
        return self.to_native(self.default())


class DynamicField(MongoDocumentField):
    type_label = 'DynamicField'
    empty = {}


class MapField(DynamicField):
    type_label = 'MapField'
