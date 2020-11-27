from functools import cached_property
from typing import Any, Dict, List

import stripe  # type: ignore
from django import forms
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http.request import HttpRequest
from django.http.response import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls.base import reverse
from django.utils import timezone
from django.views.generic.base import View
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from vdgsa_backend.accounts.models import (
    MembershipSubscription, MembershipType, PendingMembershipSubscriptionPurchase, User
)
from vdgsa_backend.accounts.views.permissions import (
    is_membership_secretary, is_requested_user_or_membership_secretary
)
from vdgsa_backend.accounts.views.utils import get_ajax_form_response


class StripeWebhookView(APIView):
    # See https://stripe.com/docs/webhooks/build#example-code
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            event = stripe.Event.construct_from(
                request.data, stripe.api_key
            )
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            return HttpResponse(str(e), status=400)

        transaction_type = event.data.object.metadata.get("transaction_type", None)
        if event.type == 'checkout.session.completed' and transaction_type == 'membership':
            with transaction.atomic():
                pending_purchase = get_object_or_404(
                    PendingMembershipSubscriptionPurchase.objects.select_for_update(),
                    stripe_payment_intent_id=event.data.object.payment_intent
                )
                if pending_purchase.is_completed:
                    return HttpResponse('Purchase already completed')
                create_or_renew_subscription(
                    pending_purchase.user, pending_purchase.membership_type)
                pending_purchase.is_completed = True
                pending_purchase.save()
                return HttpResponse('Purchase completed')

        return HttpResponse(
            f'No action taken for "{event.type}" event with transaction type "{transaction_type}"'
        )


class PurchaseSubscriptionForm(forms.Form):
    def __init__(self, user: User, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.user = user

    membership_type = forms.ChoiceField(
        choices=[
            (MembershipType.regular.value, MembershipType.regular.label),
            (MembershipType.student.value, MembershipType.student.label)
        ]
    )
    donation = forms.IntegerField(required=False, min_value=0)


class PurchaseSubscriptionView(LoginRequiredMixin, UserPassesTestMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = PurchaseSubscriptionForm(self.requested_user, request.POST)
        if not form.is_valid():
            return get_ajax_form_response(form, 400)

        # If the request sender is not the specified user, they must be the membership secretary.
        # In this case we immediately complete the purchase.
        if request.user != self.requested_user:
            create_or_renew_subscription(self.requested_user, form.cleaned_data['membership_type'])
            return get_ajax_form_response(form, 200)

        line_items = self._get_stripe_line_items(form)
        redirect_url = request.build_absolute_uri(
            reverse('user-profile', kwargs={'pk': self.requested_user.pk}))
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=line_items,
            mode='payment',
            customer_email=self.requested_user.username,
            success_url=redirect_url,
            cancel_url=redirect_url,
            metadata={'transaction_type': 'membership'}
        )

        PendingMembershipSubscriptionPurchase.objects.create(
            user=self.requested_user,
            membership_type=form.cleaned_data['membership_type'],
            stripe_payment_intent_id=session.payment_intent,
        )

        return HttpResponse(session.id, content_type='text/plain', status=202)

    def test_func(self) -> bool:
        return is_requested_user_or_membership_secretary(self.requested_user, self.request)

    @cached_property
    def requested_user(self) -> User:
        return User.objects.get(pk=self.kwargs['pk'])

    def _render_form(self, form: PurchaseSubscriptionForm) -> HttpResponse:
        return render(self.request, 'subscription/purchase_subscription.html', {'form': form})

    def _get_stripe_line_items(self, form: PurchaseSubscriptionForm) -> List[Dict[str, object]]:
        membership_type = form.cleaned_data['membership_type']
        if membership_type == MembershipType.student:
            rate = 35  # FIXME: get actual number
            label = '1-year Student Membership'
        elif membership_type == MembershipType.regular:
            rate = 40  # FIXME: get actual number
            label = '1-year Membership'
        else:
            raise ValueError(f'Unexpected membership_type: {membership_type}')

        line_items = [{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': label
                },
                'unit_amount': rate * 100  # Unit is cents
            },
            'quantity': 1,
        }]

        donation = form.cleaned_data['donation']
        if donation:
            line_items.append({
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': 'Donation'
                    },
                    'unit_amount': donation * 100  # Unit is cents
                },
                'quantity': 1,
            })

        return line_items


def _user_exists_validator(username: str) -> None:
    if not User.objects.filter(username=username).exists():
        raise ValidationError(
            f'User "{username}" does not exist. '
            'Please have them create an account before adding them as a family member.'
        )


def create_or_renew_subscription(
    user: User,
    membership_type: str
) -> MembershipSubscription:
    """
    Extends the given user's membership subscription by 1 year. If the
    user does not own a membership subscription, one is created.

    Returns the new or updated MembershipSubscription object.
    """
    if not hasattr(user, 'owned_subscription'):
        # Purchase new subscription
        now = timezone.now()
        valid_until = _plus_one_calendar_year(now)

        subscription = MembershipSubscription.objects.create(
            owner=user, valid_until=valid_until,
            membership_type=membership_type,
            years_renewed=[valid_until.year]
        )

        return subscription
    else:
        if user.owned_subscription.membership_type == MembershipType.lifetime:
            return user.owned_subscription

        # Only lifetime members should have valid_until be None.
        assert user.owned_subscription.valid_until is not None
        # Extend existing subscription by 1 year
        valid_from = user.owned_subscription.valid_until
        valid_until = _plus_one_calendar_year(user.owned_subscription.valid_until)
        user.owned_subscription.valid_until = valid_until
        user.owned_subscription.membership_type = membership_type
        user.owned_subscription.years_renewed.append(valid_from.year)
        user.owned_subscription.save()

        return user.owned_subscription


def _plus_one_calendar_year(start_at: timezone.datetime) -> timezone.datetime:
    return start_at.replace(year=start_at.year + 1)


class AddFamilyMemberForm(forms.Form):
    username = forms.EmailField(validators=[_user_exists_validator], label='Email')


class AddFamilyMemberView(LoginRequiredMixin, UserPassesTestMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = AddFamilyMemberForm(request.POST)
        if not form.is_valid():
            return get_ajax_form_response(form, 400)

        family_member = get_object_or_404(User, username=form.cleaned_data['username'])
        self.subscription.family_members.add(family_member)

        return HttpResponse()

    def test_func(self) -> bool:
        if self.subscription.owner == self.request.user:
            return True

        return is_membership_secretary(self.request.user)

    @cached_property
    def subscription(self) -> MembershipSubscription:
        return get_object_or_404(MembershipSubscription, pk=self.kwargs['pk'])


class RemoveFamilyMemberView(LoginRequiredMixin, UserPassesTestMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = User.objects.get(username=self.username)
        self.subscription.family_members.remove(user)
        return HttpResponse()

    def test_func(self) -> bool:
        if not self.subscription.family_members.filter(username=self.username).exists():
            return False

        if self.subscription.owner == self.request.user:
            return True

        return is_membership_secretary(self.request.user)

    @cached_property
    def username(self) -> str:
        return self.request.POST['username']

    @cached_property
    def subscription(self) -> MembershipSubscription:
        return get_object_or_404(MembershipSubscription, pk=self.kwargs['pk'])
