import json
import re
from copy import copy
from datetime import date, datetime, timedelta

from dbca_utils.utils import env
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.core.serializers import serialize
from django.db.models import F, Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import FormView, ListView, TemplateView, View
from indexer.utils import typesense_client
from referral.forms import (
    ClearanceCreateForm,
    IntersectingReferralForm,
    LocationForm,
    ReferralCreateForm,
    TagReplaceForm,
    TaskCancelForm,
    TaskCompleteForm,
    TaskCreateForm,
    TaskForm,
    TaskInheritForm,
    TaskReassignForm,
    TaskStartForm,
    TaskStopForm,
)
from referral.models import (
    Agency,
    Bookmark,
    Clearance,
    Condition,
    Location,
    Note,
    Organisation,
    Record,
    Referral,
    ReferralType,
    RelatedReferral,
    Task,
    TaskState,
    TaskType,
)
from referral.utils import breadcrumbs_li, is_model_or_string, is_prs_power_user, prs_user, query_caddy, smart_truncate, wfs_getfeature
from referral.views_base import PrsObjectCreate, PrsObjectDelete, PrsObjectDetail, PrsObjectList, PrsObjectUpdate
from taggit.models import Tag


class SiteHome(LoginRequiredMixin, ListView):
    """Site home page view. Returns an object list of tasks (ongoing or stopped)."""

    stopped_tasks = False
    printable = False

    def get_queryset(self):
        qs = Task.objects.current().filter(assigned_user=self.request.user)
        if self.stopped_tasks:
            qs = qs.filter(state__name="Stopped").order_by("stop_date")
        else:
            qs = qs.filter(state__is_ongoing=True)
        return qs

    def get_template_names(self):
        if "print" in self.request.GET or self.printable:
            return "site_home_print.html"
        else:
            return "site_home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stopped_tasks"] = self.stopped_tasks
        context["headers"] = copy(Task.get_headers_site_home())
        if not self.stopped_tasks:
            context["stopped_tasks_exist"] = Task.objects.current().filter(assigned_user=self.request.user, state__name="Stopped").exists()
        # Printable view only: pop the last element from 'headers'
        if "print" in self.request.GET or self.printable:
            context["headers"].pop()
        context["page_title"] = settings.APPLICATION_ACRONYM
        context["breadcrumb_trail"] = breadcrumbs_li([(None, "Home")])
        return context


class HelpPage(LoginRequiredMixin, TemplateView):
    """Help page (static template view)."""

    template_name = "help_page.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, "Help"])
        links = [(reverse("site_home"), "Home"), (None, "Help")]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        pu_group = Group.objects.get(name=settings.PRS_POWER_USER_GROUP)
        context["power_users"] = pu_group.user_set.filter(is_active=True)
        context["geoserver_url"] = settings.GEOSERVER_URL
        return context


class IndexSearch(LoginRequiredMixin, TemplateView):
    template_name = "referral/prs_index_search.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if "collection" in kwargs:
            collection = kwargs["collection"]
        else:
            collection = "referrals"

        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, f"Search {collection}"])
        context["page_heading"] = f"SEARCH {collection}".upper()
        links = [(reverse("site_home"), "Home"), (None, f"Search {collection}")]
        context["breadcrumb_trail"] = breadcrumbs_li(links)

        # Search results
        if "q" in self.request.GET and self.request.GET["q"]:
            context["query_string"] = self.request.GET["q"]
            context["search_result"] = []
            client = typesense_client()
            page = self.request.GET.get("page", 1)
            search_q = {
                "q": self.request.GET["q"],
                "sort_by": "created:desc",
                "num_typos": 0,
                "page": page,
                "per_page": 20,
            }

            if collection == "referrals":
                context["result_headers"] = Referral.get_headers()
                search_q["query_by"] = "reference,description,address,type,referring_org,lga"
            elif collection == "records":
                context["result_headers"] = Record.get_headers()
                search_q["query_by"] = "name,description,file_name,file_content"
            elif collection == "notes":
                context["result_headers"] = Note.get_headers()
                search_q["query_by"] = "note"
            elif collection == "tasks":
                context["result_headers"] = Task.get_headers()
                search_q["query_by"] = "description,assigned_user"
            elif collection == "conditions":
                context["result_headers"] = Condition.get_headers()
                search_q["query_by"] = "proposed_condition,approved_condition"

            search_result = client.collections[collection].documents.search(search_q)
            context["search_result_count"] = search_result["found"]
            paginator = Paginator(tuple(_ for _ in range(search_result["found"])), 20)
            context["page_obj"] = paginator.get_page(page)

            if collection == "referrals":
                for hit in search_result["hits"]:
                    highlights = []
                    for key, value in hit["highlight"].items():
                        # Replace underscores in search field names with spaces.
                        highlights.append((key.replace("_", " "), value["snippet"]))
                    hit["highlights"] = highlights
                    context["search_result"].append(
                        {
                            "object": Referral.objects.get(pk=hit["document"]["id"]),
                            "highlights": highlights,
                        }
                    )
            elif collection == "records":
                for hit in search_result["hits"]:
                    highlights = []
                    for key, value in hit["highlight"].items():
                        highlights.append((key.replace("_", " "), value["snippet"]))
                    hit["highlights"] = highlights
                    context["search_result"].append(
                        {
                            "object": Record.objects.get(pk=hit["document"]["id"]),
                            "highlights": highlights,
                        }
                    )
            elif collection == "notes":
                for hit in search_result["hits"]:
                    highlights = []
                    for key, value in hit["highlight"].items():
                        highlights.append((key.replace("_", " "), value["snippet"]))
                    hit["highlights"] = highlights
                    context["search_result"].append(
                        {
                            "object": Note.objects.get(pk=hit["document"]["id"]),
                            "highlights": highlights,
                        }
                    )
            elif collection == "tasks":
                for hit in search_result["hits"]:
                    highlights = []
                    for key, value in hit["highlight"].items():
                        highlights.append((key.replace("_", " "), value["snippet"]))
                    hit["highlights"] = highlights
                    context["search_result"].append(
                        {
                            "object": Task.objects.get(pk=hit["document"]["id"]),
                            "highlights": highlights,
                        }
                    )
            elif collection == "conditions":
                for hit in search_result["hits"]:
                    highlights = []
                    for key, value in hit["highlight"].items():
                        highlights.append((key.replace("_", " "), value["snippet"]))
                    hit["highlights"] = highlights
                    context["search_result"].append(
                        {
                            "object": Condition.objects.get(pk=hit["document"]["id"]),
                            "highlights": highlights,
                        }
                    )

        return context


class IndexSearchCombined(LoginRequiredMixin, TemplateView):
    """A combined version of the index search which returns referrals with linked objects."""

    template_name = "referral/prs_index_search_combined.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, "Search"])
        context["page_heading"] = "SEARCH EVERYTHING"
        links = [(reverse("site_home"), "Home"), (None, "Search")]
        context["breadcrumb_trail"] = breadcrumbs_li(links)

        # Search results
        if "q" in self.request.GET and self.request.GET["q"]:
            context["query_string"] = self.request.GET["q"]
            context["search_result"] = []
            context["referral_headers"] = Referral.get_headers()
            client = typesense_client()
            search_q = {
                "q": self.request.GET["q"],
                "sort_by": "created:desc",
                "num_typos": 0,
            }
            referrals = {}

            # Referrals
            search_q["query_by"] = "reference,description,address,type,referring_org,lga"
            search_result = client.collections["referrals"].documents.search(search_q)
            context["referrals_count"] = search_result["found"]
            for hit in search_result["hits"]:
                # Explanation for the line below: the Typesense API search response
                # returns a list of document resources, each containing a `highlight` key
                # that consists of a dict which contains 1+ `<field_name>` keys, each of which
                # consists of a dict containing the text snippet and matched tokens.
                # Earlier (<0.25) versions of the API returned `highlights` as a list instead
                # of `highlight` as this dict.
                # This is REALLY AWKWARD in this instance, as the highlight(s) will be
                # for some random `field_name`. So the next line just dumps out the FIRST
                # element in the `highlight` dict for usage below (we only need one).
                # Very occasionally, Typesense returns a blank dictionary for `highlight`
                # that causes a StopIteration exception (hence the try-except).
                try:
                    highlight = next(iter(hit["highlight"].values()))
                    ref = Referral.objects.get(pk=hit["document"]["id"])
                    referrals[ref.pk] = {
                        "referral": ref,
                        "highlight": highlight["snippet"],
                        "records": [],
                        "notes": [],
                        "tasks": [],
                        "conditions": [],
                    }
                except:
                    pass

            # Records
            search_q["query_by"] = "name,description,file_name,file_content"
            search_result = client.collections["records"].documents.search(search_q)
            context["records_count"] = search_result["found"]
            for hit in search_result["hits"]:
                try:
                    highlight = next(iter(hit["highlight"].values()))
                    ref = Referral.objects.get(pk=hit["document"]["referral_id"])
                    if ref.pk in referrals:
                        referrals[ref.pk]["records"].append((hit["document"]["id"], highlight["snippet"]))
                    else:
                        referrals[ref.pk] = {
                            "referral": ref,
                            "highlight": {},
                            "records": [(hit["document"]["id"], highlight["snippet"])],
                            "notes": [],
                            "tasks": [],
                            "conditions": [],
                        }
                except:
                    pass

            # Notes
            search_q["query_by"] = "note"
            search_result = client.collections["notes"].documents.search(search_q)
            context["notes_count"] = search_result["found"]
            for hit in search_result["hits"]:
                try:
                    highlight = next(iter(hit["highlight"].values()))
                    ref = Referral.objects.get(pk=hit["document"]["referral_id"])
                    if ref.pk in referrals:
                        referrals[ref.pk]["notes"].append((hit["document"]["id"], highlight["snippet"]))
                    else:
                        referrals[ref.pk] = {
                            "referral": ref,
                            "highlight": {},
                            "records": [],
                            "notes": [(hit["document"]["id"], highlight["snippet"])],
                            "tasks": [],
                            "conditions": [],
                        }
                except:
                    pass

            # Tasks
            search_q["query_by"] = "description,assigned_user"
            search_result = client.collections["tasks"].documents.search(search_q)
            context["tasks_count"] = search_result["found"]
            for hit in search_result["hits"]:
                try:
                    highlight = next(iter(hit["highlight"].values()))
                    ref = Referral.objects.get(pk=hit["document"]["referral_id"])
                    if ref.pk in referrals:
                        referrals[ref.pk]["tasks"].append((hit["document"]["id"], highlight["snippet"]))
                    else:
                        referrals[ref.pk] = {
                            "referral": ref,
                            "highlight": {},
                            "records": [],
                            "notes": [],
                            "tasks": [(hit["document"]["id"], highlight["snippet"])],
                            "conditions": [],
                        }
                except:
                    pass

            # Conditions
            search_q["query_by"] = "proposed_condition,approved_condition"
            search_result = client.collections["conditions"].documents.search(search_q)
            context["conditions_count"] = search_result["found"]
            for hit in search_result["hits"]:
                try:
                    if "referral_id" in hit["document"]:
                        ref = Referral.objects.get(pk=hit["document"]["referral_id"])
                        highlight = next(iter(hit["highlight"].values()))
                        if ref.pk in referrals:
                            referrals[ref.pk]["conditions"].append((hit["document"]["id"], highlight["snippet"]))
                        else:
                            referrals[ref.pk] = {
                                "referral": ref,
                                "highlight": {},
                                "records": [],
                                "notes": [],
                                "tasks": [],
                                "conditions": [(hit["document"]["id"], highlight["snippet"])],
                            }
                except:
                    pass

            # Combine the results into the template context (sort referrals by descending ID).
            for result in sorted(referrals.items(), reverse=True):
                context["search_result"].append(result[1])

        return context


class ReferralCreate(PrsObjectCreate):
    """Dedicated create view for new referrals."""

    model = Referral
    form_class = ReferralCreateForm
    template_name = "referral/referral_create.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "referrals"}), "Referrals"),
            (None, "Create"),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        context["title"] = "CREATE A NEW REFERRAL"
        context["page_title"] = "PRS | Referrals | Create"
        # Pass in a serialised list of tag names.
        context["tags"] = json.dumps([t.name for t in Tag.objects.all().order_by("name")])
        return context

    def get_initial(self):
        initial = super().get_initial()
        initial["assigned_user"] = self.request.user
        try:
            initial["referring_org"] = Organisation.objects.current().get(name__iexact="western australian planning commission")
            initial["task_type"] = TaskType.objects.get(name="Assess a referral")
            initial["agency"] = Agency.objects.get(code="DBCA")
        except Exception:
            initial["referring_org"] = Organisation.objects.current()[0]
            initial["task_type"] = TaskType.objects.all()[0]
            initial["agency"] = Agency.objects.all()[0]
        return initial

    def form_valid(self, form):
        req = self.request
        d = form.cleaned_data
        new_ref = form.save(commit=False)
        new_ref.creator, new_ref.modifier = req.user, req.user
        new_ref.save()
        form.save_m2m()  # Save any DoP Triggers.
        # All new referrals get a default task, chosen from the select list.
        new_task = Task(
            type=d["task_type"],
            referral=new_ref,
            start_date=new_ref.referral_date,
            description=new_ref.address or "",
            assigned_user=d["assigned_user"],
        )
        new_task.state = new_task.type.initial_state
        if new_ref.description:
            new_task.description += " " + new_ref.description
        # Check if a due date was provided. If not, fall back to the
        # target days defined for the Task Type.
        if d["due_date"]:
            new_task.due_date = d["due_date"]
        else:
            new_task.due_date = date.today() + timedelta(days=new_task.type.target_days)
        new_task.creator, new_task.modifier = req.user, req.user
        new_task.save()
        # If the user checked the "Email user" box, send an email notification.
        if req.POST.get("email_user"):
            subject = f"PRS new referral notification ({new_ref.id}), reference: {new_ref.reference}"
            from_email = req.user.email
            to_email = new_task.assigned_user.email
            ref_url = settings.SITE_URL + new_ref.get_absolute_url()
            address = new_ref.address or "(not recorded)"
            text_content = f"""This is an automated message to let you know that
                you have been assigned a PRS task by the sending user.\n
                This task is attached to referral ID {new_ref.pk}.\n
                The referrer's reference is {new_ref.reference}.\n
                The referral address is {address}\n
                """
            html_content = f"""<p>This is an automated message to let you know
                that you have been assigned a PRS task by the sending user.</p>
                <p>This task is attached to referral ID {new_ref.pk}</a>, at this URL:</p>
                <p>{ref_url}</p>
                <p>The referrer's reference is: {new_ref.reference}.</p>
                <p>The referral address is {address}.</p>
                """
            msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
            msg.attach_alternative(html_content, "text/html")
            # Email should fail gracefully - ie no Exception raised on failure.
            msg.send(fail_silently=True)

        messages.success(req, "New referral created successfully.")
        # If the user clicked 'Save', redirect to the new referral detail view.
        # Otherwise, assume that they clicked 'Save and add location'.
        if req.POST.get("save"):
            return redirect(new_ref.get_absolute_url())
        else:
            return redirect(reverse("referral_location_create", kwargs={"pk": new_ref.pk}))


class ReferralDetail(PrsObjectDetail):
    """Detail view for a single referral. Also includes a queryset of related/
    child objects in context.
    """

    model = Referral
    related_model = None
    template_name = "referral/referral_detail.html"

    def dispatch(self, request, *args, **kwargs):
        # related_model is an optional 'child' of referral (e.g. task, note, etc).
        # Defaults to 'task'.
        if "related_model" in kwargs:
            self.related_model = kwargs["related_model"]
        else:
            self.related_model = "tasks"
        return super().dispatch(request, *args, **kwargs)

    def get_template_names(self):
        if "print" in self.request.GET:
            if self.request.GET["print"] == "notes":
                return ["referral/referral_notes_print.html"]
        return super().get_template_names()

    def get(self, request, *args, **kwargs):
        ref = self.get_object()
        # Deleted? Redirect home.
        if ref.is_deleted():
            messages.warning(self.request, f"Referral {ref.pk} not found.")
            return HttpResponseRedirect(reverse("site_home"))

        # Update the user's referral history.
        request.user.userprofile.update_referral_history(ref)

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        ref = self.get_object()
        context["title"] = f"REFERRAL DETAILS: {ref.pk}"
        context["page_title"] = f"PRS | Referrals | {ref.pk}"
        context["rel_model"] = self.related_model
        # Test if the user has bookmarked this referral.
        if Bookmark.objects.current().filter(referral=ref, user=self.request.user).exists():
            context["bookmark"] = Bookmark.objects.filter(referral=ref, user=self.request.user)[0]

        # Generate a table for each child model type: task_list, note_list, etc. and add to the context.
        for m in [Task, Note, Record, Location, Condition]:
            obj_tab = f"tab_{m._meta.model_name}"
            obj_list = f"{m._meta.model_name}_list"
            if m.objects.current().filter(referral=ref):
                obj_name = m._meta.object_name.lower()
                context[f"{obj_name}_count"] = m.objects.current().filter(referral=ref).count()
                obj_qs = m.objects.current().filter(referral=ref)
                if m is Record:  # Sort records newest > oldest (nulls last).
                    obj_qs = obj_qs.order_by(F("order_date").desc(nulls_last=True))
                headers = copy(m.get_headers())
                headers.remove("Referral ID")
                headers.append("Actions")
                # Construct the <thead> element.
                thead = "".join([f"<th>{header}</th>" for header in headers])
                # Construct the <tbody> element.
                rows = [f"<tr>{obj.as_row_minus_referral()}{obj.as_row_actions()}</tr>" for obj in obj_qs]
                tbody = "".join(rows)
                # Construct the <table> element.
                table_html = f"""<table class="table table-striped table-bordered table-condensed prs-object-table">
                <thead><tr>{thead}</tr></thead><tbody>{tbody}<tbody></table>"""
                if m == Location:  # Append a div for the map viewer.
                    table_html += '<div id="ref_locations"></div>'
                obj_tab_html = mark_safe(table_html)
                context[obj_tab] = obj_tab_html
                context[obj_list] = obj_qs
            else:
                context[f"{m._meta.object_name.lower()}_count"] = 0
                context[obj_tab] = f"No {m._meta.verbose_name_plural} found for this referral"
                context[obj_list] = None

        # Add child locations serialised as GeoJSON (if geometry exists).
        if any([loc.poly for loc in ref.location_set.current()]):
            context["geojson_locations"] = serialize("geojson", ref.location_set.current(), geometry_field="poly", srid=4283)

        context["has_conditions"] = ref.condition_set.exists()
        return context


class ReferralCreateChild(PrsObjectCreate):
    """View to create 'child' objects for a referral, e.g. a Task or Note.
    Also allows the creation of relationships between children (e.g relating
    a Note to a Record).
    """

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        referral_id = self.parent_referral.pk
        child_model = is_model_or_string(self.kwargs["model"].capitalize())
        if "id" in self.kwargs:  # Relating to existing object.
            child_obj = str(child_model.objects.get(pk=self.kwargs["id"]))
            if self.kwargs["type"] == "addnote":
                context["title"] = f"ADD EXISTING NOTE(S) TO {child_obj.upper()}"
                context["page_title"] = f"PRS | Add note(s) to {child_obj}"
                last_breadcrumb = f"Add note(s) to {child_obj}"
            elif "addrecord" in self.kwargs.values():
                context["title"] = f"ADD EXISTING RECORD(S) TO {child_obj.upper()}"
                context["page_title"] = f"PRS | Add record(s) to {child_obj}"
                last_breadcrumb = f"Add record(s) to {child_obj}"
            elif "addnewnote" in self.kwargs.values():
                context["title"] = f"ADD NOTE TO {child_obj.upper()}"
                context["page_title"] = f"PRS | Add note to {child_obj}"
                last_breadcrumb = f"Add note to {child_obj}"
            elif "addnewrecord" in self.kwargs.values():
                context["title"] = f"ADD RECORD TO {child_obj.upper()}"
                context["page_title"] = f"PRS | Add record to {child_obj}"
                last_breadcrumb = f"Add record to {child_obj}"
        else:  # New child object.
            # Special case: clearance request task
            if "type" in self.kwargs and self.kwargs["type"] == "clearance":
                child_model = "Clearance Request"
            else:
                child_model = self.kwargs["model"].capitalize()
            context["title"] = f"CREATE A NEW {child_model.upper()}"
            context["page_title"] = f"PRS | Create {child_model}"
            last_breadcrumb = f"Create {child_model}"
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "referrals"}), "Referrals"),
            (reverse("referral_detail", kwargs={"pk": referral_id}), referral_id),
            (None, last_breadcrumb),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        if child_model == "Location":
            context["include_map"] = True
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        referral = self.parent_referral
        if "clearance" in self.kwargs.values():
            kwargs["condition_choices"] = self.get_condition_choices()
            kwargs["initial"] = {"assigned_user": self.request.user}
        if "addrecord" in self.kwargs.values() or "addnote" in self.kwargs.values():
            kwargs["referral"] = referral
        return kwargs

    @property
    def parent_referral(self):
        return Referral.objects.get(pk=self.kwargs["pk"])

    def get(self, request, *args, **kwargs):
        # Sanity check: disallow addition of clearance tasks where no approved
        # conditions exist on the referral.
        if "clearance" in self.kwargs.values() and not self.get_condition_choices():
            messages.error(self.request, "This referral has no approval conditions!")
            return HttpResponseRedirect(self.get_success_url())
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # If the user clicked Cancel, redirect to the referral detail page.
        if request.POST.get("cancel"):
            return HttpResponseRedirect(self.parent_referral.get_absolute_url())
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        self.object = form.save(commit=False)
        self.object.referral = self.parent_referral
        self.object.user = self.request.user
        self.object.creator, self.object.modifier = self.request.user, self.request.user
        redirect_url = None

        if form.Meta.model._meta.model_name == "task":
            # Create one or more clearance request tasks:
            if "type" in self.kwargs and self.kwargs["type"] == "clearance":
                self.create_clearance(form)
            # Create a task, type unspecified:
            elif "type" not in self.kwargs:
                self.create_task(self.object)
        elif form.Meta.model._meta.model_name == "record":
            # Creating a new record and relating it to another object:
            if "type" in self.kwargs and self.kwargs["type"] == "addnewrecord":
                redirect_url = self.create_new_record(form)
            # Using an existing record and relating it to another object:
            elif "type" in self.kwargs and self.kwargs["type"] == "addrecord":
                self.create_existing_record(form)
            # Creating a new record:
            elif "type" not in self.kwargs:
                self.object.save()
        elif form.Meta.model._meta.model_name == "note":
            # Creating a new note and relating it to another object:
            if "type" in self.kwargs and self.kwargs["type"] == "addnewnote":
                redirect_url = self.create_new_note(form)
            # Using an existing note and relating it to another object:
            elif "type" in self.kwargs and self.kwargs["type"] == "addnote":
                self.create_existing_note(form)
            # Creating a new note:
            elif "type" not in self.kwargs:
                self.object.save()
        elif form.Meta.model._meta.model_name == "condition":
            redirect_url = self.create_condition(self.object)
        else:
            self.object.save()

        if not messages.get_messages(self.request):
            messages.success(self.request, f"{self.object} has been created.")

        redirect_url = redirect_url if redirect_url else self.get_success_url()
        return HttpResponseRedirect(redirect_url)

    def get_success_url(self):
        if "id" in self.kwargs:  # Relating to existing object.
            child_model = is_model_or_string(self.kwargs["model"].capitalize())
            child_obj = child_model.objects.get(pk=self.kwargs["id"])
            return child_obj.get_absolute_url()
        return self.parent_referral.get_absolute_url()

    def create_existing_note(self, form):
        pk = self.kwargs["id"]
        model_name = self.model._meta.model_name  # same as self.kwargs['model']
        obj = get_object_or_404(self.model, pk=pk)  # task, note, record obj etc

        # Relate existing note(s) to the task.
        d = form.cleaned_data
        for note in d["notes"]:
            obj.notes.add(note)
            # Create the reverse relationship too.
            if model_name == "task":
                note.task_set.add(obj)
            else:
                note.records.add(obj)
        messages.success(self.request, f"The note has been added to {self.kwargs['model'].capitalize()} {self.kwargs['id']}.")

    def create_new_note(self, form):
        request = self.request

        pk = self.kwargs["id"]
        model_name = self.model._meta.model_name  # same as self.kwargs['model']
        obj = get_object_or_404(self.model, pk=pk)  # task, note, record obj etc
        referral = obj.referral

        new_note = form.save(commit=False)
        # Create a new note and relate it to the task.
        new_note.referral = referral  # Use the parent referral for the record.
        new_note.creator, new_note.modifier = request.user, request.user
        new_note.save()
        obj.notes.add(new_note)
        # Create the reverse relationship too.
        if model_name == "task":
            new_note.task_set.add(obj)

        messages.success(self.request, "New note successfully added to task.")
        redirect_url = None
        if request.POST.get("save-another"):
            redirect_url = reverse(
                "referral_create_child_related",
                kwargs={
                    "pk": referral.pk,
                    "id": pk,
                    "model": "task",
                    "type": "addnewnote",
                },
            )
        return redirect_url

    def create_existing_record(self, form):
        pk = self.kwargs["id"]
        model_name = self.model._meta.model_name  # same as self.kwargs['model']
        obj = get_object_or_404(self.model, pk=pk)  # task, note, record obj etc

        # Relate existing record(s) to the obj.
        d = form.cleaned_data
        for record in d["records"]:
            obj.records.add(record)
            # Create the reverse relationship too.
            if model_name == "task":
                record.task_set.add(obj)
            else:
                record.note_set.add(obj)
        messages.success(self.request, f"The record has been added to {self.kwargs["model"].capitalize()} {self.kwargs["id"]}.")

    def create_new_record(self, form):
        request = self.request
        pk = self.kwargs["id"]
        model_name = self.model._meta.model_name  # same as self.kwargs['model']
        obj = get_object_or_404(self.model, pk=pk)  # task, note, record obj etc
        referral = obj.referral
        new_record = form.save(commit=False)
        # Create a new record and relate it to the obj.
        new_record.referral = referral  # Use the parent referral for the record.
        new_record.creator, new_record.modifier = request.user, request.user
        new_record.save()
        obj.records.add(new_record)
        # Create the reverse relationship too.
        if model_name == "task":
            new_record.task_set.add(obj)

        redirect_url = None
        if request.POST.get("save-another"):
            redirect_url = reverse(
                "referral_create_child_related",
                kwargs={
                    "pk": referral.pk,
                    "id": pk,
                    "model": model_name,
                    "type": "addnewrecord",
                },
            )
        return redirect_url

    def get_condition_choices(self):
        """Return conditions with 'approved' text only."""
        condition_qs = Condition.objects.current().filter(referral=self.parent_referral).exclude(condition="")
        condition_choices = []
        for i in condition_qs:
            condition_choices.append((i.id, f"{i.identifier or ""} - {smart_truncate(i.condition, 100)}"))
        return condition_choices

    def create_clearance(self, form):
        """For each of the chosen conditions in the form, create one clearance task."""
        request = self.request

        tasks = []
        for i in form.cleaned_data["conditions"]:
            condition = Condition.objects.get(pk=i)
            clearance_task = Task()
            clearance_task.type = TaskType.objects.get(name="Conditions clearance request")
            clearance_task.referral = condition.referral
            clearance_task.assigned_user = form.cleaned_data["assigned_user"]
            clearance_task.start_date = form.cleaned_data["start_date"]
            clearance_task.description = form.cleaned_data["description"]
            clearance_task.state = clearance_task.type.initial_state
            if form.cleaned_data["due_date"]:
                clearance_task.due_date = form.cleaned_data["due_date"]
            else:
                clearance_task.due_date = date.today() + timedelta(days=clearance_task.type.target_days)
            clearance_task.creator, clearance_task.modifier = request.user, request.user
            clearance_task.save()
            condition.add_clearance(task=clearance_task, deposited_plan=form.cleaned_data["deposited_plan"])
            # If the user checked the "Email user" box, send them a notification.
            if request.POST.get("email_user"):
                subject = f"PRS referral {clearance_task.referral.pk} - new clearance request notification"
                from_email = request.user.email
                to_email = clearance_task.assigned_user.email
                referral_url = settings.SITE_URL + clearance_task.referral.get_absolute_url()
                text_content = f"""This is an automated message to let you know that you have
                    been assigned PRS clearance request {clearance_task.pk} by the sending user.\n
                    This clearance request is attached to referral ID {clearance_task.referral.pk}.\n
                    """
                html_content = f"""<p>This is an automated message to let you know that you have
                    been assigned PRS clearance request {clearance_task.pk} by the sending user.</p>
                    <p>This task is attached to referral ID {clearance_task.referral.pk}</a>, at this URL:</p>
                    <p>{referral_url}</p>"""
                msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
                msg.attach_alternative(html_content, "text/html")
                # Email should fail gracefully - ie no Exception raised on failure.
                msg.send(fail_silently=True)
            tasks.append(clearance_task.pk)

        messages.success(self.request, f"New Clearance {str(tasks).strip("[]")} has been created.")

    def create_condition(self, obj):
        obj.save()
        messages.success(self.request, f"{obj} has been created.")
        # If no model condition was chosen, email users in the 'PRS power user' group.
        pu_group = Group.objects.get(name=settings.PRS_POWER_USER_GROUP)
        users = pu_group.user_set.filter(is_active=True)
        subject = "PRS condition created notification"
        from_email = "PRS-Alerts@dbca.wa.gov.au"
        for user in users:
            # Send a single email to this user
            to_email = [user.email]
            text_content = """This is an automated message to let you know
                that the following condition was just created by:\n"""
            html_content = """<p>This is an automated message to let you know
                that the following condition was just created:</p>"""
            text_content += f"* Condition ID {obj.pk}\n"
            html_content += f"<p><a href='{settings.SITE_URL}{obj.get_absolute_url()}'>Condition ID {obj.pk}</a></p>"
            text_content += f"The condition was created by {obj.creator.get_full_name()}.\n"
            html_content += f"<p>The condition was created by {obj.creator.get_full_name()}.</p>"
            text_content += "This is an automatically-generated email - please do not reply.\n"
            html_content += "<p>This is an automatically-generated email - please do not reply.</p>"
            msg = EmailMultiAlternatives(subject, text_content, from_email, to_email)
            msg.attach_alternative(html_content, "text/html")
            # Email should fail gracefully - ie no Exception raised on failure.
            msg.send(fail_silently=True)
        redirect_url = None
        if self.request.POST.get("save-another"):
            referral = obj.referral
            redirect_url = reverse(
                "referral_create_child",
                kwargs={"pk": referral.pk, "model": "condition"},
            )
        return redirect_url

    def create_task(self, obj):
        # Set the default initial state for the task type.
        obj.state = obj.type.initial_state

        # Auto-complete certain task types.
        if obj.type.name in [
            "Information only",
            "Provide pre-referral/preliminary advice",
        ]:
            obj.due_date = date.today()
            obj.complete_date = date.today()
            obj.state = TaskState.objects.get(name="Complete")

        obj.save()

        # If "email user" was checked, do so now.
        if self.request.POST.get("email_user"):
            obj.email_user()


class LocationCreate(ReferralCreateChild):
    """Specialist view to allow selection of locations from cadastre, or
    digitisation of a spatial area.
    """

    model = Location
    form_class = LocationForm
    template_name = "referral/location_create.html"

    @property
    def parent_referral(self):
        return get_object_or_404(Referral, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        # Standard view context data.
        self.kwargs["model"] = "location"  # Append to kwargs
        context = super().get_context_data(**kwargs)
        ref = self.parent_referral
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "referrals"}), "Referrals"),
            (reverse("referral_detail", kwargs={"pk": ref.pk}), ref.pk),
            (None, "Create locations(s)"),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        context["title"] = "CREATE LOCATION(S)"
        context["address"] = ref.address
        # Add any existing referral locations serialised as GeoJSON.
        if any([loc.poly for loc in ref.location_set.current()]):
            context["geojson_locations"] = serialize("geojson", ref.location_set.current(), geometry_field="poly", srid=4283)
        return context

    def get_success_url(self):
        return reverse("referral_detail", kwargs={"pk": self.parent_referral.pk})

    def post(self, request, *args, **kwargs):
        if request.POST.get("cancel"):
            return HttpResponseRedirect(self.get_success_url())
        ref = self.parent_referral

        # Aggregate the submitted form values into a dict of dicts.
        forms = {}
        for key, val in request.POST.items():
            if key.startswith("form-"):
                form = re.findall("^form-[0-9]+", key)[0]
                field = re.sub("^form-[0-9]+-", "", key)
                if form in forms:  # Form dict already started.
                    pass
                else:  # Start a new form dict.
                    forms[form] = {}
                forms[form][field] = val

        # Iterate through forms, create new location for each.
        locations = []
        for form in forms.values():
            wkt = form.pop("wkt")
            poly = GEOSGeometry(wkt)
            # Set any blank form field values to None (digitised features)
            for k, v in form.items():
                if not v:
                    form[k] = None
            # EDGE CASE: very occasionally, address_no now contains non-numeric character.
            # If so, remove the non-numeric characters.
            if "address_no" in form and form["address_no"]:
                form["address_no"] = re.sub(r"\D", "", form["address_no"])
            loc = Location(**form)
            if isinstance(poly, MultiPolygon):
                loc.poly = poly[0]
            else:
                loc.poly = poly
            loc.referral = ref
            loc.save()
            locations.append(loc)

        messages.success(request, f"{len(forms)} location(s) created.")

        # Test for intersecting locations.
        intersecting_locations = self.polygon_intersects(locations)
        if intersecting_locations:
            # Redirect to a view where users can create relationships between referrals.
            locs_str = "_".join(map(str, intersecting_locations))
            return HttpResponseRedirect(
                reverse(
                    "referral_intersecting_locations",
                    kwargs={"pk": ref.id, "loc_ids": locs_str},
                )
            )
        else:
            return HttpResponseRedirect(self.get_success_url())

    def polygon_intersects(self, locations):
        """Check to see if the location polygon intersects with any other locations."""
        intersecting_locations = []
        for location in locations:
            if location.poly:
                other_locs = Location.objects.current().exclude(pk=location.pk).filter(poly__isnull=False, poly__intersects=location.poly)
                if other_locs.exists():
                    intersecting_locations.append(location.pk)
        return intersecting_locations


class LocationIntersects(PrsObjectCreate):
    model = Referral
    form_class = IntersectingReferralForm

    @property
    def parent_referral(self):
        return self.get_object()

    def get_success_url(self):
        return reverse("referral_detail", kwargs={"pk": self.parent_referral.pk})

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["referral"] = self.parent_referral
        kwargs["referrals"] = self.referral_intersecting_locations()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        referral = self.parent_referral
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "referrals"}), "Referrals"),
            (reverse("referral_detail", kwargs={"pk": referral.pk}), referral.pk),
            (None, "Referrals with intersect(s)"),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        context["title"] = "REFERRALS WITH INTERSECTING LOCATION(S)"
        return context

    def post(self, request, *args, **kwargs):
        # If the user clicked Cancel, redirect to the referral detail page.
        if request.POST.get("cancel"):
            return HttpResponseRedirect(self.get_success_url())
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        # For each intersecting referral chosen, add a relationship
        for ref in form.cleaned_data["related_refs"]:
            self.parent_referral.add_relationship(ref)

        messages.success(self.request, f"{len(form.cleaned_data["related_refs"])} Referral relationship(s) created.")
        return redirect(self.get_success_url())

    def referral_intersecting_locations(self):
        # get the loc_ids string and convert to list of int's
        loc_ids = map(int, self.kwargs["loc_ids"].split("_"))

        referral_ids = []
        for loc_id in loc_ids:
            location = get_object_or_404(Location, pk=loc_id)
            geom = location.poly.wkt
            intersects = Location.objects.current().exclude(id=location.id).filter(poly__isnull=False)
            # Get a qs of intersecting locations
            intersects = intersects.filter(poly__intersects=geom)
            # Get a qs of referrals
            for i in intersects:
                # Don't add the passed-in referral to the list.
                if i.referral.id != self.parent_referral.id:
                    referral_ids.append(i.referral.id)

        unique_referral_ids = list(set(referral_ids))
        return Referral.objects.current().filter(id__in=unique_referral_ids)


class RecordUpload(LoginRequiredMixin, View):
    """Minimal view to handle POST of form-encoded uploaded files.
    Files can be uploaded on a Referral (creates a new child Record), or
    directly on a Record (updates it to replace any previous uploaded file).
    """

    http_method_names = ["post"]
    parent_referral = False

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        if not prs_user(request):
            return HttpResponseForbidden("You do not have permission to create records")
        return super().dispatch(request, *args, **kwargs)

    def get_parent_object(self):
        if self.parent_referral:
            return Referral.objects.get(pk=self.kwargs["pk"])
        else:
            return Record.objects.get(pk=self.kwargs["pk"])

    def post(self, request, *args, **kargs):
        if "file" not in request.FILES:
            return HttpResponseBadRequest("File not found")

        uploaded_file = request.FILES["file"]
        if uploaded_file.content_type not in settings.ALLOWED_UPLOAD_TYPES:
            return HttpResponseBadRequest("Disallowed file type")

        if self.parent_referral:
            rec = Record(
                name=uploaded_file.name,
                referral=self.get_parent_object(),
                uploaded_file=uploaded_file,
                creator=request.user,
                modifier=request.user,
            )
        else:
            rec = self.get_parent_object()
            rec.uploaded_file = uploaded_file
            rec.modifier = request.user

        # Non *.msg files only: set order_date to today's date.
        if rec.extension != "MSG":
            rec.order_date = date.today()

        rec.save()

        return JsonResponse(
            {
                "success": True,
                "object": {"id": rec.pk, "resource_uri": rec.get_absolute_url()},
            }
        )


class TaskAction(PrsObjectUpdate):
    """
    A customised view is used for editing Tasks because of the additional business logic.
    ``action`` includes add, stop, start, reassign, complete, cancel, inherit, update,
    addrecord, addnewrecord, addnote, addnewnote
    NOTE: does not include the 'delete' action (handled by separate view).
    """

    model = Task
    template_name = "referral/change_form.html"
    action = None

    def get(self, request, *args, **kwargs):
        """Business rule/sanity check on task state (disallow some state
        changes for tasks). Ensures that actions that shouldn't be able to
        occur, don't occur. E.g. can't stop a task that is already stopped
        or already completed.
        """
        action = self.kwargs["action"]
        task = self.get_object()

        if action == "update" and task.stop_date and not task.restart_date:
            messages.error(request, "You can't edit a stopped task - restart the task first!")
            return redirect(task.get_absolute_url())
        if action == "stop" and task.complete_date:
            messages.error(request, "You can't stop a completed task!")
            return redirect(task.get_absolute_url())
        if action == "start" and not task.stop_date:
            messages.error(request, "You can't restart a non-stopped task!")
            return redirect(task.get_absolute_url())
        if action == "inherit" and task.assigned_user == request.user:
            messages.info(request, "That task is already assigned to you.")
            return redirect(task.get_absolute_url())
        if action in ["reassign", "complete", "cancel"] and task.complete_date:
            messages.info(request, "That task is already completed.")
            return redirect(task.get_absolute_url())
        # We can't (yet) add a task to a task.
        if action == "add":
            return redirect(task.get_absolute_url())
        # Business rule: adding a location is mandatory before completing some
        # 'Assess' tasks.
        trigger_ref_type = ReferralType.objects.filter(
            name__in=[
                "Development application",
                "Drain/pump/take water, watercourse works",
                "Extractive industry / mining",
                "GBRS amendment",
                "Land tenure change",
                "Management plan / technical report",
                "MRS amendment",
                "Pastoral lease permit to diversify",
                "Planning scheme / amendment",
                "PRS amendment",
                "Structure Plan",
                "Subdivision",
                "Utilities infrastructure & roads",
                "Clearing Permit - DMIRS",
                "Clearing Permit - DWER",
                "LSU - Amendments to Reserves or UCL",
                "LSU - Leases or easements over crown land",
                "LSU - Road actions",
                "LSU - s91 licences",
                "LSU - s121 diversification permits",
            ]
        )
        if action == "complete" and task.referral.type in trigger_ref_type and not task.referral.has_location:
            url = reverse("referral_location_create", kwargs={"pk": task.referral.pk})
            msg = f"You are unable to complete this task without first recording location(s) on the referral. <a href='{url}'>Click here to create location(s).</a>"
            messages.warning(self.request, msg)
            return redirect(task.get_absolute_url())
        return super().get(request, *args, **kwargs)

    def get_form_class(self):
        action = self.kwargs["action"]
        if action == "stop":
            return TaskStopForm
        elif action == "start":
            return TaskStartForm
        elif action == "inherit":
            return TaskInheritForm
        elif action == "complete":
            return TaskCompleteForm
        elif action == "cancel":
            return TaskCancelForm
        elif action == "reassign":
            return TaskReassignForm
        elif action == "add":
            return TaskCreateForm
        else:
            return TaskForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Create a breadcrumb trail: Home[URL] > Tasks[URL] > ID[URL] > Action
        action = self.kwargs["action"]
        obj = self.get_object()
        context["page_title"] = f"PRS | Tasks | {obj.pk} | {action.capitalize()}"
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "tasks"}), "Tasks"),
            (
                reverse("prs_object_detail", kwargs={"pk": obj.pk, "model": "tasks"}),
                obj.pk,
            ),
            (None, action.capitalize()),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        context["title"] = action.upper() + " TASK"
        # Pass in a serialised list of tag names.
        context["tags"] = json.dumps([t.name for t in Tag.objects.all().order_by("name")])
        return context

    def get_success_url(self):
        task = self.get_object()
        return task.referral.get_absolute_url()

    def post(self, request, *args, **kwargs):
        # If the user clicked Cancel, redirect back to the site home page.
        if request.POST.get("cancel"):
            return redirect("site_home")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        action = self.kwargs["action"]
        obj = form.save(commit=False)
        d = form.cleaned_data

        # This is where custom logic for the different actions takes place (if required).
        if action == "stop":
            obj.stop_date = d["stopped_date"]
            obj.state = TaskState.objects.all().get(name="Stopped")
            obj.restart_date = None
        elif action == "start":
            obj.state = obj.type.initial_state
            obj.stop_time = obj.stop_time + (obj.restart_date - obj.stop_date).days
        elif action == "inherit":
            obj.assigned_user = self.request.user
        elif action == "cancel":
            obj.state = TaskState.objects.all().get(name="Cancelled")
            obj.complete_date = datetime.now()
        elif action == "reassign":
            if self.request.POST.get("email_user"):
                obj.email_user()
        elif action == "update":
            if obj.restart_date and obj.stop_date:
                obj.stop_time = (obj.restart_date - obj.stop_date).days
        elif action == "complete":
            if obj.type.name == "Assess a referral":
                # Rule: proposed condition is mandatory for some 'Assess' task outcomes.
                trigger_outcome = TaskState.objects.filter(name__in=["Response with condition"])
                trigger_ref_type = ReferralType.objects.filter(
                    name__in=[
                        "Development application",
                        "Extractive industry / mining",
                        "Subdivision",
                    ]
                )
                if obj.state in trigger_outcome and obj.referral.type in trigger_ref_type and not obj.referral.has_proposed_condition:
                    url = reverse(
                        "referral_create_child",
                        kwargs={"pk": obj.referral.pk, "model": "condition"},
                    )
                    msg = f"""You are unable to complete this task as 'Response with conditions' without first recording proposed
                        condition(s) on the referral. <a href="{url}">Click here to create a condition.</a>"""
                    messages.warning(self.request, mark_safe(msg))
                    return redirect(obj.get_absolute_url())
                # Rule: >0 Tags are mandatory for some 'Assess' task outcomes.
                trigger_outcome = TaskState.objects.filter(
                    name__in=[
                        "Insufficient information provided",
                        "Response with advice",
                        "Response with condition",
                        "Response with objection",
                    ]
                )
                form_data = form.cleaned_data
                if obj.state in trigger_outcome and not form_data["tags"]:
                    msg = """You are unable to select that task outcome without
                        recording tags that are relevant to advice provided."""
                    messages.warning(self.request, msg)
                    return self.form_invalid(form)
                # Save selected tags on the task's parent referral.
                if form_data["tags"]:
                    for tag in form_data["tags"]:
                        obj.referral.tags.add(tag)

        obj.modifier = self.request.user
        obj.save()
        return super().form_valid(form)


class ReferralRecent(PrsObjectList):
    """Override the general-purpose list view to return only referrals in the
    user's recent history.
    """

    model = Referral
    paginate_by = None
    template_name = "referral/referral_recent.html"

    def get_queryset(self):
        if not self.request.user.userprofile.referral_history_array:
            return Referral.objects.none()

        return Referral.objects.current().filter(pk__in=self.request.user.userprofile.referral_history_array)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        title = "Recent referrals"
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, title])
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "referrals"}), "Referrals"),
            (None, "Recent referrals"),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        return context


class ReferralReferenceSearch(PrsObjectList):
    """
    This is a small utility view that returns a small template meant to inserted
    inline to a form during an AJAX call when adding a new referral.
    The template is rendered with an object list of referrals returned whose
    reference matches that input for the new referral

    E.g. if the new referrals reference in "1234", this view will return all
    existing referrals with "1234" inside their reference too.
    """

    model = Referral
    template_name = "referral/referral_reference_search.html"

    def get_queryset(self):
        object_count = 0
        q = self.request.GET.get("q")
        queryset = Referral.objects.current().filter(Q(reference__contains=q))
        object_count = queryset.count()
        # If we have a lot of results, slice and return the first twenty only.
        if object_count > 20:
            queryset = queryset[0:19]
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["object_count"] = self.object_list.count()
        return context


class TagList(PrsObjectList):
    """Custom view to return a readonly list of tags (rendered HTML or JSON)."""

    model = Tag
    template_name = "referral/tag_list.html"
    http_method_names = ["get", "options"]

    def get(self, request, *args, **kwargs):
        """For an AJAX request or one containing a ``json`` request parameter,
        return a JSON response (list of tag names).
        """
        if "json" in request.GET:
            loc = [str(i) for i in self.get_queryset().values_list("name", flat=True)]
            return JsonResponse(loc, safe=False)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return Tag.objects.all().order_by("name")


class TagReplace(LoginRequiredMixin, FormView):
    """Custom view to replace all instances of a tag with another.
    NOTE: only users in the 'PRS power user' group can access this view.
    """

    form_class = TagReplaceForm
    template_name = "referral/change_form.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_superuser and not is_prs_power_user(request):
            return HttpResponseForbidden("You do not have permission to use this function.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, "Replace tag"])
        context["title"] = "REPLACE TAG"
        return context

    def post(self, request, *args, **kwargs):
        # If the user clicks "Cancel", redirect to the tags list view.
        if request.POST.get("cancel"):
            return HttpResponseRedirect(reverse("tag_list"))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        d = form.cleaned_data
        old, new = d["old_tag"], d["replace_with"]
        # Iterate through all model classes that use tags.
        # Replace old tags with new tags.
        for model in [Referral, Condition]:
            tagged = model.objects.filter(tags__name__in=[old.name])
            for obj in tagged:
                obj.tags.remove(old)
                obj.tags.add(new)
        # Finally, delete the old tag
        old.delete()
        messages.success(self.request, f"All '{old}' tags have been replaced by '{new}'")
        return HttpResponseRedirect(reverse("tag_list"))


class ReferralTagged(PrsObjectList):
    """Override the Referral model list view to filter tagged objects."""

    model = Referral

    def get_queryset(self):
        qs = super().get_queryset()
        if not Tag.objects.filter(slug=self.kwargs["slug"]).exists():
            return []
        # Filter queryset by the tag.
        tag = Tag.objects.get(slug=self.kwargs["slug"])
        qs = qs.filter(tags__in=[tag]).distinct()
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not Tag.objects.filter(slug=self.kwargs["slug"]).exists():
            title = f"Referrals tagged: {self.kwargs['slug']}"
        else:
            tag = Tag.objects.get(slug=self.kwargs["slug"])
            title = f"Referrals tagged: {tag.name}"
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, title])
        links = [
            (reverse("site_home"), "Home"),
            (reverse("prs_object_list", kwargs={"model": "referral"}), "Referrals"),
            (None, title),
        ]
        context["breadcrumb_trail"] = breadcrumbs_li(links)
        context["object_type_plural"] = title
        return context


class BookmarkList(PrsObjectList):
    """Override to default object list to only show current user bookmarks."""

    model = Bookmark

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.filter(user=self.request.user)
        return qs


class ReferralDelete(PrsObjectDelete):
    model = Referral
    template_name = "referral/prs_object_delete.html"

    def get_success_url(self):
        return reverse("site_home")

    def get_context_data(self, **kwargs):
        ref = self.get_object()

        context = super().get_context_data(**kwargs)
        context["object"] = ref
        context["object_type_plural"] = self.model._meta.verbose_name_plural
        context["object_type"] = self.model._meta.verbose_name
        return context

    def post(self, request, *args, **kwargs):
        ref = self.get_object()
        if request.POST.get("cancel"):
            return redirect("site_home")

        # Delete referral relationships
        # We can just call delete on this queryset.
        RelatedReferral.objects.filter(Q(from_referral=ref) | Q(to_referral=ref)).delete()
        # Delete any tags on the referral
        ref.tags.clear()
        # Delete tasks
        # Need iterate this queryset to call the object delete() method
        tasks = Task.objects.current().filter(referral=ref)
        for i in tasks:
            i.delete()
        # Delete records
        records = Record.objects.current().filter(referral=ref)
        for i in records:
            i.delete()
        # Delete notes
        notes = Note.objects.current().filter(referral=ref)
        for i in notes:
            i.delete()
        # Delete conditions
        conditions = Condition.objects.current().filter(referral=ref)
        for i in conditions:
            # Delete any clearances on each condition
            # We can just call delete on this queryset.
            Clearance.objects.current().filter(condition=i).delete()
            i.delete()
        # Delete locations
        locations = Location.objects.current().filter(referral=ref)
        for i in locations:
            i.delete()
        # Delete bookmarks
        bookmarks = Bookmark.objects.current().filter(referral=ref)
        for i in bookmarks:
            i.delete()
        ref.delete()
        messages.success(request, f"{self.model._meta.object_name} deleted.")

        return redirect("site_home")


class ReferralRelate(PrsObjectList):
    """Custom list view to search referrals to relate together."""

    model = Referral
    template_name = "referral/referral_relate.html"
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def get_object(self):
        return Referral.objects.get(pk=self.kwargs["pk"])

    def get_queryset(self):
        # Exclude parent object from queryset.
        qs = super().get_queryset()
        return qs.exclude(pk=self.get_object().pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        title = "Add a related referral"
        context["object_type_plural"] = title.upper()
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, title])
        context["referral"] = self.get_object()
        return context

    def post(self, request, *args, **kwargs):
        """Handle POST requests to create or delete referral relationships.
        Expects a kwarg ``pk`` to define the 'first' referral, plus query
        parameters ``ref_pk`` and EITHER ``create`` or ``delete``.

        ``ref_pk``: PK of the 'second' referral.
        ``create``: create a relationship
        ``delete``: delete the relationship
        """
        # NOTE: query parameters always live in request.GET.
        if not self.request.GET.get("ref_pk", None):
            raise AttributeError(f"Relate view {self.__class__.__name__} must be called with a " "ref_pk query parameter.")

        if "create" not in self.request.GET and "delete" not in self.request.GET:
            raise AttributeError(f"Relate view {self.__class__.__name__} must be called with either " "create or delete query parameters.")

        ref1 = self.get_object()
        ref2 = get_object_or_404(Referral, pk=self.request.GET.get("ref_pk"))

        if prs_user(request):
            if "create" in self.request.GET:
                ref1.add_relationship(ref2)
                messages.success(request, "Referral relation created")
            elif "delete" in self.request.GET:
                ref1.remove_relationship(ref2)
                messages.success(request, "Referral relation removed")

        return redirect(ref1.get_absolute_url())


class ReferralLocationDownload(View):
    """Basic view to generate spatial data for a given referral's location, and return it as a download.
    Several formats are available via the `format` request parameter, otherwise defaults to GeoJSON.
    """

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        referral = get_object_or_404(Referral, pk=self.kwargs["pk"])

        # Deleted? Redirect home.
        if referral.is_deleted():
            messages.warning(self.request, f"Referral {referral.pk} not found.")
            return HttpResponseRedirect(reverse("site_home"))

        # No locations? Redirect to the referral detail view.
        if not referral.location_set.current().exists():
            return HttpResponseRedirect(referral.get_absolute_url())

        if "format" in request.GET and request.GET["format"] == "qgis":
            content = referral.generate_qgis_layer()
            filename = f"prs_referral_{referral.pk}.qlr"
            resp = HttpResponse(content, content_type="application/x-qgis-project")
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp
        elif "format" in request.GET and request.GET["format"] == "gpkg":
            content = referral.generate_gpkg(source_url=request.build_absolute_uri(location=referral.get_absolute_url()))
            filename = f"prs_referral_{referral.pk}.gpkg"
            resp = HttpResponse(content, content_type="application/x-sqlite3")
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp
        else:
            content = referral.generate_geojson(source_url=request.build_absolute_uri(location=referral.get_absolute_url()))
            filename = f"prs_referral_{referral.pk}.geojson"
            resp = HttpResponse(content, content_type="application/geo+json")
            resp["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp


class ConditionClearanceCreate(PrsObjectCreate):
    """
    View to add a clearance request to a single condition object.
    This view opens the form for adding a new condition clearance to the database.
    ``pk`` is the PK of the Condition to which the clearance request is being made.
    """

    model = Condition
    form_class = ClearanceCreateForm
    template_name = "referral/change_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        condition = get_object_or_404(Condition, pk=self.kwargs["pk"])
        kwargs.update({"condition": condition})
        return kwargs

    def get_object(self):
        return Condition.objects.current().get(pk=self.kwargs.get("pk"))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        context["title"] = "CREATE A CLEARANCE REQUEST"
        title = "Create clearance request"
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, title])
        model_list_url = reverse("prs_object_list", kwargs={"model": "conditions"})
        context["breadcrumb_trail"] = breadcrumbs_li(
            [
                (reverse("site_home"), "Home"),
                (model_list_url, "Conditions"),
                (obj.get_absolute_url, str(obj.pk)),
                (None, "Create a clearance request"),
            ]
        )
        return context

    def get_initial(self):
        initial = super().get_initial()
        obj = self.get_object()
        initial["assigned_user"] = self.request.user
        initial["description"] = obj.condition
        return initial

    def post(self, request, *args, **kwargs):
        obj = self.get_object()

        # On Cancel, redirect to the Condition URL.
        if request.POST.get("cancel"):
            return HttpResponseRedirect(reverse("prs_object_detail", kwargs={"pk": obj.pk, "model": "conditions"}))

        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        obj = self.get_object()
        clearance_task = form.save(commit=False)
        clearance_task.type = TaskType.objects.get(name="Conditions clearance request")
        clearance_task.referral = obj.referral
        clearance_task.state = clearance_task.type.initial_state
        if form.cleaned_data["due_date"]:
            clearance_task.due_date = form.cleaned_data["due_date"]
        else:
            clearance_task.due_date = date.today() + timedelta(days=clearance_task.type.target_days)
        clearance_task.creator, clearance_task.modifier = (
            self.request.user,
            self.request.user,
        )
        clearance_task.save()
        obj.add_clearance(task=clearance_task, deposited_plan=form.cleaned_data["deposited_plan"])
        messages.success(self.request, "New clearance request created successfully.")

        # If the user check the "Email user" box, send them a notification.
        if self.request.POST.get("email_user"):
            subject = f"PRS referral {clearance_task.referral.pk} - new clearance request notification"
            from_email = self.request.user.email
            to_email = clearance_task.assigned_user.email
            referral_url = settings.SITE_URL + clearance_task.referral.get_absolute_url()
            text_content = f"""This is an automated message to let you know that you have
                been assigned PRS clearance request {clearance_task.pk} by the sending user.\n
                This clearance request is attached to referral ID {clearance_task.referral.pk}.\n"""
            html_content = f"""<p>This is an automated message to let you know that you have
                been assigned PRS clearance request {clearance_task.pk} by the sending user.</p>
                <p>This task is attached to referral ID {clearance_task.referral.pk}, at this URL:</p>
                <p>{referral_url}</p>"""
            msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
            msg.attach_alternative(html_content, "text/html")
            # Email should fail gracefully - ie no Exception raised on failure.
            msg.send(fail_silently=True)

        # Business rule: for each new clearance request, email users in the PRS power users group.
        subject = f"PRS referral {clearance_task.referral.pk} - new condition clearance request notification"
        from_email = "PRS-Alerts@dbca.wa.gov.au"
        pu_group = Group.objects.get(name=settings.PRS_POWER_USER_GROUP)
        to_email = [user.email for user in pu_group.user_set.filter(is_active=True)]

        text_content = "This is an automated message to let you know that the following clearance request was just created:\n"
        html_content = "<p>This is an automated message to let you know that the following clearance request was just created:</p>"
        text_content += f"* Task ID {clearance_task.pk}\n"
        html_content += f"<p><a href='{settings.SITE_URL}{clearance_task.get_absolute_url()}'>Task ID {clearance_task.pk}</a></p>"
        text_content += f"The clearance task was created by {clearance_task.creator.get_full_name()}.\n"
        html_content += f"<p>The clearance task was created by {clearance_task.creator.get_full_name()}.</p>"
        text_content += "This is an automatically-generated email - please do not reply.\n"
        html_content += "<p>This is an automatically-generated email - please do not reply.</p>"
        msg = EmailMultiAlternatives(subject, text_content, from_email, to_email)
        msg.attach_alternative(html_content, "text/html")
        # Email should fail gracefully - i.e. no Exception raised on failure.
        msg.send(fail_silently=True)

        return redirect(clearance_task.get_absolute_url())


class InfobaseShortcut(View):
    """Basic view to generate a shortcut file to an Infobase object
    The file is a one-line text file containing the Infobase ID, with a .obr
    file extension.
    """

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        record = get_object_or_404(Record, pk=self.kwargs["pk"])
        if record.infobase_id:
            response = HttpResponse(content_type="application/octet-stream")
            response["Content-Disposition"] = f"attachment; filename=infobase_{record.infobase_id}.obr"
            # The HttpResponse is a file-like object; write the Infobase ID and return it.
            response.write(record.infobase_id)
            return response
        else:
            messages.warning(request, "That record is not associated with an InfoBase object ID.")
            return HttpResponseRedirect(record.get_absolute_url())


class CadastreQuery(View):
    """Basic view endpoint to send a CQL filter query to the Cadastre spatial service."""

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        cql_filter = request.GET.get("cql_filter", None)
        if not cql_filter:
            # This view requires a CQL filter value.
            return HttpResponseBadRequest("Bad request")
        crs = request.GET.get("crs", None)
        type_name = env("CADASTRE_LAYER_NAME")
        if crs:
            resp = wfs_getfeature(type_name, cql_filter, crs)
        else:
            resp = wfs_getfeature(type_name, cql_filter)

        return JsonResponse(resp)


class ReferralMap(LoginRequiredMixin, TemplateView):
    """A map view displaying all referral locations."""

    template_name = "referral/referral_map.html"
    http_method_names = ["get"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = " | ".join([settings.APPLICATION_ACRONYM, "Referrals map"])
        return context


class GeocodeQuery(View):
    """Basic view endpoint to send a geocode query to the Caddy spatial service."""

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        q = request.GET.get("q", None)

        if not q:
            return HttpResponseBadRequest("Bad request")
        resp = query_caddy(q)
        return JsonResponse(resp, safe=False)
