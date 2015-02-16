from __future__ import unicode_literals

from mongoengine.errors import ValidationError as me_ValidationError
from mongoengine import fields as me_fields
from mongoengine.base.common import get_document

from django.db import models
from django.forms import widgets
from django.core.exceptions import ImproperlyConfigured

from collections import OrderedDict

from rest_framework import serializers
from rest_framework import fields as drf_fields
from rest_framework.fields import SkipField
from rest_framework_mongoengine.utils import get_field_info, FieldInfo
from rest_framework_mongoengine.fields import (ReferenceField, ListField, EmbeddedDocumentField, DynamicField,
                                               ObjectIdField, DocumentField, BinaryField, BaseGeoField, get_field_mapping, is_drfme_field)
from rest_framework_mongoengine.fields import ME_FIELD_MAPPING
import copy


def raise_errors_on_nested_writes(method_name, serializer, validated_data):
    """
    *** inherited from DRF 3, altered for EmbeddedDocumentSerializer to work automagically ***

    Give explicit errors when users attempt to pass writable nested data.

    If we don't do this explicitly they'd get a less helpful error when
    calling `.save()` on the serializer.

    We don't *automatically* support these sorts of nested writes because
    there are too many ambiguities to define a default behavior.

    Eg. Suppose we have a `UserSerializer` with a nested profile. How should
    we handle the case of an update, where the `profile` relationship does
    not exist? Any of the following might be valid:

    * Raise an application error.
    * Silently ignore the nested part of the update.
    * Automatically create a profile instance.
    """

    # Ensure we don't have a writable nested field. For example:
    #
    # class UserSerializer(ModelSerializer):
    #     ...
    #     profile = ProfileSerializer()
    assert not any(
        isinstance(field, serializers.BaseSerializer) and
        not isinstance(field, EmbeddedDocumentSerializer) and
        (key in validated_data)
        for key, field in serializer.fields.items()
    ), (
        'The `.{method_name}()` method does not support writable nested'
        'fields by default.\nWrite an explicit `.{method_name}()` method for '
        'serializer `{module}.{class_name}`, or set `read_only=True` on '
        'nested serializer fields.'.format(
            method_name=method_name,
            module=serializer.__class__.__module__,
            class_name=serializer.__class__.__name__
        )
    )

    # Ensure we don't have a writable dotted-source field. For example:
    #
    # class UserSerializer(ModelSerializer):
    #     ...
    #     address = serializer.CharField('profile.address')
    assert not any(
        '.' in field.source and (key in validated_data)
        for key, field in serializer.fields.items()
    ), (
        'The `.{method_name}()` method does not support writable dotted-source '
        'fields by default.\nWrite an explicit `.{method_name}()` method for '
        'serializer `{module}.{class_name}`, or set `read_only=True` on '
        'dotted-source serializer fields.'.format(
            method_name=method_name,
            module=serializer.__class__.__module__,
            class_name=serializer.__class__.__name__
        )
    )


class DocumentSerializer(serializers.ModelSerializer):
    """

    Model Serializer that supports Mongoengine
    DRF - 3 comes with pretty cool new features and more elegant and readable codebase.
    So it was not that hard to hack the way through mongoengine compability.

    To users:
        MongoEngineModelSerializer is now DocumentSerializer
        everything works on MongoEngineModelSerializer works on DocumentSerializer as well, even more performant.

        You can also use nested DocumentSerializers, just like on DRF3 ModelSerializer.

        DocumentSerializer takes care of EmbeddedDocumentField, ListField, ReferenceField automatically.
        If you want some custom behavior, you should implement
        a nested serializer with .create() .update() methods set up

    To contributors:
         Before start of development, please consider reading DRF 3 ModelSerializer implementation

         - The process order like is_valid() -> run_validation() -> to_internal_value() is crucial when
         implementing custom behavior.

         - Important to understand that all Mongoengine Fields are converted to DRF fields on the go.
         The fields that require custom implementation(like ListField), we convert them to our
         custom DocumentField(or any subclass).

         All contributions are welcome.

         Here is a to-do list if you consider contributing
            - implement better get_fields()
            - make sure all kwargs (regarding validation/serialization) on models
              passes to serializers on get_field_kwargs()
            - check and implement ChoiceField on DRF
            - check if DRF validators work correctly
            - write tests
            - check and resolve issues
            - maybe a better way to implement transform_%s methods on fields.py

    """
    def __init__(self, instance=None, data=serializers.empty, **kwargs):
        super(DocumentSerializer, self).__init__(instance=instance, data=data, **kwargs)
        if not hasattr(self.Meta, 'model'):
            raise AssertionError('You should set `model` attribute on %s.' % type(self).__name__)

    MAX_RECURSION_DEPTH = 5  # default value of depth

    embedded_document_serializer_fields = []

    def get_validators(self):
        validators = getattr(getattr(self, 'Meta', None), 'validators', [])
        return validators

    def get_field_kwargs(self, model_field):
        """
        Get kwargs that will be used for validation/serialization
        """
        kwargs = {}

        #kwargs to pass to all drfme fields
        #this includes lists, dicts, embedded documents, etc
        #depth included for flow control during recursive serialization.
        if is_drfme_field(model_field):
            kwargs['model_field'] = model_field
            kwargs['depth'] = getattr(self.Meta, 'depth', self.MAX_RECURSION_DEPTH)

        if type(model_field) is me_fields.ObjectIdField:
            kwargs['required'] = False
        else:
            kwargs['required'] = model_field.required

        if model_field.default:
            kwargs['required'] = False
            kwargs['default'] = model_field.default

        attribute_dict = {
            me_fields.StringField: ['max_length'],
            me_fields.DecimalField: ['min_value', 'max_value'],
            me_fields.EmailField: ['max_length'],
            me_fields.FileField: ['max_length'],
            me_fields.URLField: ['max_length'],
            me_fields.BinaryField: ['max_bytes']
        }

        #append any extra attributes based on the dict above, as needed.
        if model_field.__class__ in attribute_dict:
            attributes = attribute_dict[model_field.__class__]
            for attribute in attributes:
                if hasattr(model_field, attribute):
                    kwargs.update({attribute: getattr(model_field, attribute)})

        if model_field.__class__ is me_fields.StringField:
            kwargs['allow_null'] = not kwargs['required']
            kwargs['allow_blank'] = not kwargs['required']

        return kwargs

    def get_field_info(self, model):
        return get_field_info(model)

    def get_fields(self):
        #fields declared on Serializer (e.g. Name = CharField() in class definition)
        declared_fields = copy.deepcopy(self._declared_fields)

        #instantiate return OrderedDictionary
        ret = OrderedDict()

        #get info from Meta
        model = getattr(self.Meta, 'model') #model for serializer
        fields = getattr(self.Meta, 'fields', None) #explicit list of fields
        exclude = getattr(self.Meta, 'exclude', None) #list of fields to exclude
        depth = getattr(self.Meta, 'depth', 0) #depth to crawl to
        extra_kwargs = getattr(self.Meta, 'extra_kwargs', {}) #extra kwargs.

        #format extra kwargs
        extra_kwargs = self._include_additional_options(extra_kwargs)

        #check fields and exclude, make sure they didn't do anything stupid
        if fields and not isinstance(fields, (list, tuple)):
            raise TypeError(
                'The `fields` option must be a list or tuple. Got %s.' %
                type(fields).__name__
            )

        if exclude and not isinstance(exclude, (list, tuple)):
            raise TypeError(
                'The `exclude` option must be a list or tuple. Got %s.' %
                type(exclude).__name__
            )

        assert not (fields and exclude), "Cannot set both 'fields' and 'exclude'."

        # # Retrieve metadata about fields & relationships on the model class.
        info = self.get_field_info(model)

        # Use the default set of field names if none is supplied explicitly.
        if fields is None:
            #no fields set, so define the defaults.
            fields = self._get_default_field_names(declared_fields, info)

            #if serializer lists fields to exclude, drop them from the list.
            if exclude is not None:
                for field_name in exclude:
                    fields.remove(field_name)


        # Determine the set of model fields, and the fields that they map to.
        # We actually only need this to deal with the slightly awkward case
        # of supporting `unique_for_date`/`unique_for_month`/`unique_for_year`.
        model_field_mapping = {}
        embedded_list = []
        #for all fields we're going to serialize..
        for field_name in fields:
            if field_name in declared_fields:
                #if we declared it, get the source from the field we declared (or use field_name as default)
                field = declared_fields[field_name]
                source = field.source or field_name
                if isinstance(field, EmbeddedDocumentSerializer):
                    embedded_list.append(field)
            else:
                #fields we didn't define in Serializer class (and are being built from the model)
                #get source from extra_kwargs, or use field_name as default.
                try:
                    source = extra_kwargs[field_name]['source']
                except KeyError:
                    source = field_name
            # Model fields will always have a simple source mapping,
            # they can't be nested attribute lookups.
            if '.' not in source and source != '*':
                model_field_mapping[source] = field_name

        #only includes EmbeddedDocumentSerializers specifically defined in declared_fields
        #everything else will be using an EmbeddedDocumentField (or similar)
        self.embedded_document_serializer_fields = embedded_list

        #Now determine the fields that should be included on the serializer.
        for field_name in fields:
            if field_name in declared_fields:
                # Field is explicitly declared on the class, use that.
                ret[field_name] = declared_fields[field_name]
                continue

            elif field_name in info.fields_and_pk:
                # Create regular model fields.
                model_field = info.fields_and_pk[field_name]
                field_cls = get_field_mapping(model_field)
                if field_cls is None:
                    raise KeyError('%s is not supported, yet. Please open a ticket regarding '
                                   'this issue and have it fixed asap.\n'
                                   'https://github.com/umutbozkurt/django-rest-framework-mongoengine/issues/' %
                                   type(model_field))

                kwargs = self.get_field_kwargs(model_field)

            elif hasattr(model, field_name):
                #if not a field, but exists on the model,
                # Create a read only field for model methods and properties.
                field_cls = drf_fields.ReadOnlyField
                kwargs = {}

            else:
                #bad ju-ju. Shouldn't get here (as all fields should be explicitly declared or part of model
                raise ImproperlyConfigured(
                    'Field name `%s` is not valid for model `%s`.' %
                    (field_name, model.__class__.__name__)
                )

            # Check that any fields declared on the class are
            # also explicitly included in `Meta.fields`.
            missing_fields = set(declared_fields.keys()) - set(fields)
            if missing_fields:
                missing_field = list(missing_fields)[0]
                raise ImproperlyConfigured(
                    'Field `%s` has been declared on serializer `%s`, but '
                    'is missing from `Meta.fields`.' %
                    (missing_field, self.__class__.__name__)
                )

            # Populate any kwargs defined in `Meta.extra_kwargs`
            extras = extra_kwargs.get(field_name, {})
            if extras.get('read_only', False):
                #if defined as read_only, drop any kwargs that don't work with that
                #from kwargs that will be passed to field's init
                for attr in [
                    'required', 'default', 'allow_blank', 'allow_null',
                    'min_length', 'max_length', 'min_value', 'max_value',
                    'validators', 'queryset'
                ]:
                    kwargs.pop(attr, None)

            if extras.get('default') and kwargs.get('required') is False:
                kwargs.pop('required')

            kwargs.update(extras)

            # Create the serializer field, finally.
            ret[field_name] = field_cls(**kwargs)

        return ret

    def is_valid(self, raise_exception=False):
        """
        Call super.is_valid() and then apply embedded document serializer's validations.
        """
        valid = super(DocumentSerializer, self).is_valid(raise_exception=raise_exception)

        for embedded_field in self.embedded_document_serializer_fields:
            embedded_field._initial_data = self.validated_data.pop(embedded_field.field_name, serializers.empty)
            valid &= embedded_field.is_valid(raise_exception=raise_exception)

        return valid

    def to_representation(self, instance):
        """
        Object instance -> Dict of primitive datatypes.
        """
        #instantiate return dict
        ret = OrderedDict()

        #get list of fields from self.fields.values()
        fields = [field for field in self.fields.values() if not field.write_only]

        for field in fields:
                try:
                    #get attribute from field
                    #probably primitive datatype for simple fields (text, int, etc)
                    #possibly something more complicated for objects, lists, or whatnot.
                    attribute = field.get_attribute(instance)
                except SkipField:
                    continue

                if attribute is None:
                    # We skip `to_representation` for `None` values so that
                    # fields do not have to explicitly deal with that case.
                    ret[field.field_name] = None
                else:
                    #pass the attribute to the to_representation function to get final representation
                    #of the data.
                    ret[field.field_name] = field.to_representation(attribute)

        return ret

    def create(self, validated_data):
        """
        Create an instance using queryset.create()
        Before create() on self, call EmbeddedDocumentSerializer's create() first. If exists.
        """
        raise_errors_on_nested_writes('create', self, validated_data)

        # Automagically create and set embedded documents to validated data
        for embedded_field in self.embedded_document_serializer_fields:
            embedded_doc_intance = embedded_field.create(embedded_field.validated_data)
            validated_data[embedded_field.field_name] = embedded_doc_intance

        ModelClass = self.Meta.model
        try:
            instance = ModelClass(**validated_data)
            instance.save()
        except TypeError as exc:
            msg = (
                'Got a `TypeError` when calling `%s.objects.create()`. '
                'This may be because you have a writable field on the '
                'serializer class that is not a valid argument to '
                '`%s.objects.create()`. You may need to make the field '
                'read-only, or override the %s.create() method to handle '
                'this correctly.\nOriginal exception text was: %s.' %
                (
                    ModelClass.__name__,
                    ModelClass.__name__,
                    type(self).__name__,
                    exc
                )
            )
            raise TypeError(msg)
        except me_ValidationError as exc:
            msg = (
                'Got a `ValidationError` when calling `%s.objects.create()`. '
                'This may be because request data satisfies serializer validations '
                'but not Mongoengine`s. You may need to check consistency between '
                '%s and %s.\nIf that is not the case, please open a ticket '
                'regarding this issue on https://github.com/umutbozkurt/django-rest-framework-mongoengine/issues'
                '\nOriginal exception was: %s' %
                (
                    ModelClass.__name__,
                    ModelClass.__name__,
                    type(self).__name__,
                    exc
                )
            )
            raise me_ValidationError(msg)

        return instance

    def update(self, instance, validated_data):
        """
        Update embedded fields first, set relevant attributes with updated data
        And then continue regular updating
        """
        for embedded_field in self.embedded_document_serializer_fields:
            embedded_doc_intance = embedded_field.update(getattr(instance, embedded_field.field_name), embedded_field.validated_data)
            setattr(instance, embedded_field.field_name, embedded_doc_intance)

        return super(DocumentSerializer, self).update(instance, validated_data)

class PolymorphicDocumentSerializer(DocumentSerializer):

    def get_field_info(self, model):

        subcls_info = {}
        for cls in model._subclasses:
            subcls_info[cls] = get_field_info(get_document(cls))

        ret = get_field_info(model)
        for cls_info in subcls_info.values():
            for field in cls_info.fields:
                if field not in ret.fields:
                    ret.fields[field] = cls_info.fields[field]

        ret.fields_and_pk.update(ret.fields)
        return ret

    def to_representation(self, instance):
        """
        Object instance -> Dict of primitive datatypes.
        """
        #instantiate return dict
        ret = OrderedDict()

        #get list of fields from self.fields.values()
        fields = {}
        for field_name, field in self.fields.iteritems():
            if not field.write_only:
                fields[field_name] = field

        for field_name in instance._fields:
            if field_name in fields:
                try:
                    #get attribute from field
                    #probably primitive datatype for simple fields (text, int, etc)
                    #possibly something more complicated for objects, lists, or whatnot.
                    attribute = fields[field_name].get_attribute(instance)
                except SkipField:
                    continue

                if attribute is None:
                    # We skip `to_representation` for `None` values so that
                    # fields do not have to explicitly deal with that case.
                    ret[fields[field_name].field_name] = None
                else:
                    #pass the attribute to the to_representation function to get final representation
                    #of the data.
                    ret[fields[field_name].field_name] = fields[field_name].to_representation(attribute)

        return ret

class DynamicDocumentSerializer(DocumentSerializer):
    """
    DocumentSerializer adjusted for DynamicDocuments.
    """
    def to_internal_value(self, data):
        """
        Dict of native values <- Dict of primitive datatypes.
        After calling super, we handle dynamic data which is not handled by super class
        """
        ret = super(DocumentSerializer, self).to_internal_value(data)
        [drf_fields.set_value(ret, [k], data[k]) for k in data if k not in ret]
        return ret

    def to_representation(self, instance):
        """
        Object instance -> Dict of primitive datatypes.
        Serialize regular + dynamic fields
        """
        ret = OrderedDict()
        fields = [field for field in self.fields.values() if not field.write_only]
        fields += self._get_dynamic_fields(instance).values()

        for field in fields:
            attribute = field.get_attribute(instance)
            if attribute is None:
                ret[field.field_name] = None
            else:
                ret[field.field_name] = field.to_representation(attribute)

        return ret

    def _get_dynamic_fields(self, document):
        dynamic_fields = {}
        if document is not None and document._dynamic:
            for name, field in document._dynamic_fields.items():
                dynamic_fields[name] = DynamicField(field_name=name, source=name, **self.get_field_kwargs(field))
        return dynamic_fields


class EmbeddedDocumentSerializer(DocumentSerializer):
    """
    A DocumentSerializer adjusted to have extended control over serialization and validation of EmbeddedDocuments.
    """

    def create(self, validated_data):
        """
        EmbeddedDocuments are not saved separately, so we create an instance of it.
        """
        raise_errors_on_nested_writes('create', self, validated_data)
        return self.Meta.model(**validated_data)

    def update(self, instance, validated_data):
        """
        EmbeddedDocuments are not saved separately, so we just update the instance and return it.
        """
        raise_errors_on_nested_writes('update', self, validated_data)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        return instance

    def _get_default_field_names(self, declared_fields, model_info):
        """
        EmbeddedDocuments don't have `id`s so do not include `id` to field names
        """
        return (
            list(declared_fields.keys()) +
            list(model_info.fields.keys()) +
            list(model_info.forward_relations.keys())
        )