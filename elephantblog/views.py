from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.db import models
from django.utils.cache import add_never_cache_headers
from django.views.generic import dates
from elephantblog.models import Category, Entry
from elephantblog.utils import entry_list_lookup_related
import datetime
try:
    from django.utils import timezone
except ImportError:
    timezone = None
    pass


try:
    from towel import paginator
except ImportError:
    from django.core import paginator

__all__ = ('ArchiveIndexView', 'YearArchiveView', 'MonthArchiveView', 'DayArchiveView',
    'DateDetailView', 'CategoryArchiveIndexView')

PAGINATE_BY = getattr(settings, 'BLOG_PAGINATE_BY', 10)

class ElephantblogMixin(object):
    """
    This mixin autodetects whether the blog is integrated through an
    ApplicationContent and automatically switches to inheritance2.0
    if that's the case.

    Additionally, it adds the view instance to the template context
    as ``view``.

    This requires at least FeinCMS v1.5.
    """

    def get_context_data(self, **kwargs):
        kwargs.update({'view': self})
        return super(ElephantblogMixin, self).get_context_data(**kwargs)

    def get_queryset(self):
        return Entry.objects.active().transform(entry_list_lookup_related)

    def render_to_response(self, context, **response_kwargs):
        if 'app_config' in getattr(self.request, '_feincms_extra_context', {}):
            return self.get_template_names(), context

        return super(ElephantblogMixin, self).render_to_response(
            context, **response_kwargs)
        

class ArchiveIndexView(ElephantblogMixin, dates.ArchiveIndexView):
    paginator_class = paginator.Paginator
    paginate_by = PAGINATE_BY
    date_field = 'published_on'
    template_name_suffix = '_archive'
    allow_empty = True


class YearArchiveView(ElephantblogMixin, dates.YearArchiveView):
    paginator_class = paginator.Paginator
    paginate_by = PAGINATE_BY
    date_field = 'published_on'
    make_object_list = True
    template_name_suffix = '_archive'


class MonthArchiveView(ElephantblogMixin, dates.MonthArchiveView):
    paginator_class = paginator.Paginator
    paginate_by = PAGINATE_BY
    month_format = '%m'
    date_field = 'published_on'
    template_name_suffix = '_archive'


class DayArchiveView(ElephantblogMixin, dates.DayArchiveView):
    paginator_class = paginator.Paginator
    paginate_by = PAGINATE_BY
    month_format = '%m'
    date_field = 'published_on'
    template_name_suffix = '_archive'


class DateDetailView(ElephantblogMixin, dates.DateDetailView):
    paginator_class = paginator.Paginator
    paginate_by = PAGINATE_BY
    month_format = '%m'
    date_field = 'published_on'

    def get_queryset(self):
        if (self.request.user.is_authenticated() and self.request.user.is_staff
                and self.request.GET.get('eb_preview')):
            return Entry.objects.all()
        return Entry.objects.active()

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        response = self.prepare()
        if response:
            return response

        response = self.render_to_response(self.get_context_data(object=self.object))
        return self.finalize(response)

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def _make_date_lookup_arg(self, value):
        """
        Available in Django >= 1.5 only
        Convert a date into a datetime when the date field is a DateTimeField.

        When time zone support is enabled, `date` is assumed to be in the UTC,
        so that displayed items are consistent with the URL.
        """
        if self.uses_datetime_field:
            value = datetime.datetime.combine(value, datetime.time.min)
            if settings.USE_TZ:
                value = timezone.make_aware(value, timezone.utc)
        return value


    def get_object(self, queryset=None):
        """
        Compat for django 1.4
        """
        # Django >= 1.5
        if hasattr(dates.DateDetailView, '_date_from_string'):
            return dates.DateDetailView.get_object(queryset)

        def _date_lookup_for_field(field, date):
            """
            Patch the function so it returns aware datetimes using UTC.
            """
            if isinstance(field, models.DateTimeField):
                date_range = (
                    timezone.make_aware(datetime.datetime.combine(
                                        date, datetime.time.min), timezone.utc),
                    timezone.make_aware(datetime.datetime.combine(
                                        date, datetime.time.max), timezone.utc)
                    )
                return {'%s__range' % field.name: date_range}
            else:
                return {field.name: date}


        year = self.get_year()
        month = self.get_month()
        day = self.get_day()
        date = dates._date_from_string(year, self.get_year_format(),
            month, self.get_month_format(),
            day, self.get_day_format())

        # Use a custom queryset if provided
        qs = queryset or self.get_queryset()

        if not self.get_allow_future() and date > datetime.date.today():
            raise Http404(_(u"Future %(verbose_name_plural)s not available because %(class_name)s.allow_future is False.") % {
                'verbose_name_plural': qs.model._meta.verbose_name_plural,
                'class_name': self.__class__.__name__,
                })

        # Filter down a queryset from self.queryset using the date from the
        # URL. This'll get passed as the queryset to DetailView.get_object,
        # which'll handle the 404
        date_field = self.get_date_field()
        field = qs.model._meta.get_field(date_field)

        if settings.USE_TZ:
            lookup = _date_lookup_for_field(field, date)
        else:
            lookup = dates._date_lookup_for_field(field, date)

        qs = qs.filter(**lookup)

        return super(dates.BaseDetailView, self).get_object(queryset=qs)


    def prepare(self):
        """
        Prepare / pre-process content types. If this method returns anything,
        it is treated as a ``HttpResponse`` and handed back to the visitor.
        """

        http404 = None     # store eventual Http404 exceptions for re-raising,
                           # if no content type wants to handle the current self.request
        successful = False # did any content type successfully end processing?

        for content in self.object.content.all_of_type(tuple(self.object._feincms_content_types_with_process)):
            try:
                r = content.process(self.request, view=self)
                if r in (True, False):
                    successful = r
                elif r:
                    return r
            except Http404, e:
                http404 = e

        if not successful:
            if http404:
                # re-raise stored Http404 exception
                raise http404

            """ XXX This does not make sense in this context, does it?
            if not settings.FEINCMS_ALLOW_EXTRA_PATH and \
                    self.request._feincms_extra_context['extra_path'] != '/':
                raise Http404
            """

    def finalize(self, response):
        """
        Runs finalize() on content types having such a method, adds headers and
        returns the final response.
        """

        if not isinstance(response, HttpResponse):
            # For example in the case of inheritance 2.0
            return response

        for content in self.object.content.all_of_type(tuple(self.object._feincms_content_types_with_finalize)):
            r = content.finalize(self.request, response)
            if r:
                return r

        # Add never cache headers in case frontend editing is active
        if hasattr(self.request, "session") and self.request.session.get('frontend_editing', False):
            add_never_cache_headers(response)

        return response


class CategoryArchiveIndexView(ArchiveIndexView):
    template_name_suffix = '_archive'

    def get_queryset(self):
        self.category = get_object_or_404(Category, translations__slug=self.kwargs['slug'])

        queryset = super(CategoryArchiveIndexView, self).get_queryset()
        return queryset.filter(categories=self.category)

    def get_context_data(self, **kwargs):
        return super(CategoryArchiveIndexView, self).get_context_data(
            category=self.category,
            **kwargs)
