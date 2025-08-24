# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

from flask import render_template, redirect, request, url_for, session
from flask import current_app as app
from flask_login import (
    current_user,
    login_user,
    logout_user,
    login_required,
)

from apps import db, login_manager, oauth, mail
from apps.config import app_config
from apps.audit_mixin import get_remote_addr, utcnow
from apps.authentication import blueprint
from apps.authentication.forms import LoginForm, CreateAccountForm, ConfirmAccountForm, ResetPasswordForm, ResetPasswordConfirmForm
from apps.authentication.models import Users, Groups, LoginLogging
from flask_mail import Message
import uuid
from datetime import timedelta
from sqlalchemy import or_
import secrets
from apps import cache

from apps.authentication.util import verify_pass


def _check_pre_approved(user):
    """Given an user, check if this is user is on the list of pre-approved users or member of any group"""
    if user.category != "user":
        return
    for group in user.member_of_groups:
        if group.organization == "SYSTEM":
            continue
        user.category = "student"
        return True
    added_group = False
    groups = Groups.query.filter(Groups.is_deleted==False, Groups.approved_users!="").all()
    for group in groups:
        if user.email in group.approved_users_list:
            user.category = "student"
            group.members.append(user)
            added_group = True
    return added_group


@blueprint.route('/')
def route_default():
    return redirect(url_for('authentication_blueprint.login'))


# Login & Registration

@blueprint.route('/login/', methods=['GET', 'POST'])
def login():
    login_form = LoginForm(request.form)
    if 'login' in request.form:

        # read form data
        identifier = request.form['identifier']
        password = request.form['password']

        # Locate user
        user = Users.query.filter(or_(Users.email == identifier, Users.username == identifier)).first()

        # Check the password
        if user and not user.is_deleted and user.password and verify_pass(password, user.password):
            _check_pre_approved(user)
            login_user(user)
            app.logger.info(f"Successful login ipaddr={get_remote_addr()} login={identifier} auth_provider=local")
            login_log = LoginLogging(ipaddr=get_remote_addr(), login=identifier, auth_provider="local", success=True)
            db.session.add(login_log)
            db.session.commit()
            if "next_url" in session:
                next_url = session.pop("next_url")
                return redirect(next_url)
            return redirect(url_for('authentication_blueprint.route_default'))

        app.logger.warn(f"Failed login ipaddr={get_remote_addr()} login={identifier} auth_provider=local")
        login_log = LoginLogging(ipaddr=get_remote_addr(), login=identifier, auth_provider="local", success=False)
        db.session.add(login_log)
        db.session.commit()

        # Something (user or pass) is not ok
        return render_template('pages/login.html',
                               msg='Wrong user or password',
                               form=login_form)

    if not current_user.is_authenticated:
        return render_template('pages/login.html',
                               form=login_form)
    return redirect(url_for('home_blueprint.index'))


@blueprint.route("/login/oauth")
def login_oauth():
    """Redirects the user to the OAUTH login page."""
    return oauth.provider.authorize_redirect(
        redirect_uri=app_config.BASE_URL + url_for("authentication_blueprint.callback", _external=False)
    )


@blueprint.route("/login/callback", methods=["GET", "POST"])
def callback():
    """Callback redirect from OAuth Provider"""
    token = oauth.provider.authorize_access_token().get("userinfo", {})
    subject = token.get("sub", None)
    issuer = token.get("iss", None)
    given_name = token.get("given_name", None)
    family_name = token.get("family_name", None)
    email = token.get("email", None)

    if not subject:
        app.logger.warn("Invalid auth token from OAUTH callback:", token)
        return render_template('pages/page-403.html'), 403

    user = Users.query.filter_by(subject=subject).first()
    if not user:
        user = Users(subject=subject, issuer=issuer, given_name=given_name, family_name=family_name, email=email)
        db.session.add(user)

    _check_pre_approved(user)
    app.logger.info(f"Successful login ipaddr={get_remote_addr()} login={subject} auth_provider={issuer} email={email}")
    login_log = LoginLogging(ipaddr=get_remote_addr(), login=subject, auth_provider=issuer, success=True)
    db.session.add(login_log)
    db.session.commit()

    login_user(user)

    if "next_url" in session:
        next_url = session.pop("next_url")
        return redirect(next_url)

    return redirect(url_for('authentication_blueprint.route_default'))

@blueprint.route('/register', methods=['GET', 'POST'])
def register():
    create_account_form = CreateAccountForm(request.form)

    if request.method == 'POST':
        if not create_account_form.validate_on_submit():
            msg = "Failed to validate form."
            if create_account_form.errors:
                msg += f" Errors: {create_account_form.errors}"
            return render_template('pages/register.html', form=create_account_form, msg=msg)

        username = create_account_form.username.data
        email = create_account_form.email.data
        password = create_account_form.password.data

        # Check username exists
        user = Users.query.filter_by(username=username).first()
        if user:
            return render_template('pages/register.html',
                                   msg='Username already registered',
                                   success=False,
                                   form=create_account_form)

        # Check email exists
        user = Users.query.filter_by(email=email).first()
        if user:
            return render_template('pages/register.html',
                                   msg='Email already registered',
                                   success=False,
                                   form=create_account_form)

        # else: send confirmation e-mail if configured or create user
        if not app.config['MAIL_SERVER']:
            user = Users(username=username, email=email, password=password, issuer="LOCAL")
            db.session.add(user)
            db.session.commit()
            return redirect(url_for('authentication_blueprint.login'))

        confirmation_token = str(uuid.uuid4().int)[:6]
        session['confirmation_token'] = confirmation_token
        session['user'] = dict(username=username, email=email, password=password, issuer="LOCAL")
        session['datetime'] = utcnow()

        # Send email
        msg = Message(
            subject="HackInSDN - Verify your identity",
            sender=app.config['MAIL_USERNAME'],
            recipients=[email],
            body=(
                "Help us protect your account\n\n"
                "Before you sign up, we need to verify your identity. Enter the following code on the sign-up page.\n\n"
                f"{confirmation_token}\n\n"
                "If you have not recently tried to sign up into HackInSDN, you can ignore this e-mail.\n\n"
                "--\n\n"
                f"You're receiving this email because of your account on Dashboard HackInSDN."
            ),
            html=render_template('mail/confirmation_token.html', confirmation_token=confirmation_token),
        )
        mail.send(msg)

        return redirect(url_for('authentication_blueprint.confirm_page'))

    else:
        return render_template('pages/register.html', form=create_account_form)

@blueprint.route('/confirm', methods=['GET', 'POST'])
def confirm_page():
    form = ConfirmAccountForm(request.form)

    confirmation_token = session.get('confirmation_token')
    if not confirmation_token:
        return redirect(url_for('authentication_blueprint.register'))
        
    if 'confirm' in request.form:
        user = Users(**session.get('user'))
        created_at = session.get('datetime')
     
        now = utcnow()
        if now - created_at > timedelta(minutes=5):
            return render_template('pages/confirm.html', msg='Token expired, please <a href=/register>click here</a> to register again', success=False, form=form)

        if request.form['confirmation_token'] != confirmation_token:
            return render_template('pages/confirm.html', msg='Invalid token', success=False, form=form)

        session.pop('confirmation_token')
        session.pop('user')
        session.pop('datetime')

        db.session.add(user)
        db.session.commit()

        return redirect(url_for('authentication_blueprint.login'))
    
    return render_template('pages/confirm.html', form=form)

@blueprint.route('/resend-code', methods=['GET'])
def resend_code():
    email = session.get('user').get('email')
    confirmation_token = session.get('confirmation_token')

    msg = Message(
        subject="HackInSDN - Verify your identity",
        sender=app.config['MAIL_USERNAME'],
        recipients=[email],
        body=(
            "Help us protect your account\n\n"
            "Before you sign up, we need to verify your identity. Enter the following code on the sign-up page.\n\n"
            f"{confirmation_token}\n\n"
            "If you have not recently tried to sign up into HackInSDN, you can ignore this e-mail.\n\n"
            "--\n\n"
            f"You're receiving this email because of your account on Dashboard HackInSDN."
        ),
        html=render_template('mail/confirmation_token.html', confirmation_token=confirmation_token),
    )
    mail.send(msg)

    return redirect(url_for('authentication_blueprint.confirm_page'))

@blueprint.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    msg = ""
    form = ResetPasswordForm(request.form)
    if not form.validate_on_submit():
        if form.errors:
            msg = f"Errors validating form: {form.errors}"
        return render_template('pages/reset_password.html', form=form, msg=msg)

    msg = "If the user provided is valid, you will receive an e-mail with password reset link."
    identifier = form.identifier.data
    user = Users.query.filter(or_(Users.email == identifier, Users.username == identifier)).first()
    if not user or user.is_deleted:
        return render_template('pages/reset_password.html', form=form, msg=msg)

    confirmation_token = secrets.token_urlsafe(64)
    confirmation_url = app_config.BASE_URL + url_for("authentication_blueprint.confirm_reset_password", token=confirmation_token)

    cache.set(f"resetpw-{confirmation_token}", user.id, timeout=3600)

    mail_msg = Message(
        subject="HackInSDN - Reset your password",
        sender=app.config['MAIL_USERNAME'],
        recipients=[user.email],
        body=(
            "Help us protect your account\n\n"
            "Your password can be reset by clicking the button below. If you did not request a new password, please ignore this email.\n\n"
            f"{confirmation_url}\n\n"
            "--\n\n"
            f"You are receiving this email because of your account on Dashboard HackInSDN."
        ),
        html=render_template('mail/reset_password.html', confirmation_url=confirmation_url),
    )
    mail.send(mail_msg)

    return render_template('pages/reset_password.html', form=form, msg=msg)

@blueprint.route('/confirm-reset-password/<token>', methods=['GET', 'POST'])
def confirm_reset_password(token):
    msg = ""
    form = ResetPasswordConfirmForm(request.form)
    resetpw_user = cache.get(f"resetpw-{token}")
    if not resetpw_user:
        app.logger.info(f"Invalid password reset request ipaddr={get_remote_addr()} token={token}")
        msg = "Invalid or expired token! You need to request a new <a href='{url_for('authentication_blueprint.reset_password')}'>Password Reset</a>"
        return render_template('pages/confirm_reset_password.html', form=None, msg=msg)

    if not form.validate_on_submit():
        if form.errors:
            msg = f"Errors validating form: {form.errors}"
        return render_template('pages/confirm_reset_password.html', form=form, msg=msg)

    cache.delete(f"resetpw-{token}")

    user = Users.query.get(resetpw_user)
    if not user or user.is_deleted:
        app.logger.info(f"Invalid password reset request ipaddr={get_remote_addr()} user={resetpw_user}")
        msg = "Invalid password reset request! You need to request a new <a href='{url_for('authentication_blueprint.reset_password')}'>Password Reset</a>"
        return render_template('pages/confirm_reset_password.html', form=None, msg=msg)

    user.set_password(form.password.data)
    db.session.commit()

    app.logger.info(f"Successful password reset ipaddr={get_remote_addr()} login={user.username} auth_provider=local")
    msg = f"Password changed successfully! Now you can <a href='{url_for('authentication_blueprint.login')}'>click to Login</a>"
    return render_template('pages/confirm_reset_password.html', form=form, msg=msg)

@blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('authentication_blueprint.login'))

@blueprint.route('/profile/reload')
@login_required
def reload_profile():
    if _check_pre_approved(current_user):
        db.session.commit()
    return redirect(url_for('authentication_blueprint.route_default'))

# Errors

@login_manager.unauthorized_handler
def unauthorized_handler():
    if 'login' not in request.path:
        session['next_url'] = request.path
    return redirect(url_for('authentication_blueprint.login'))


@blueprint.errorhandler(403)
def access_forbidden(error):
    return render_template('pages/page-403.html'), 403


@blueprint.errorhandler(404)
def not_found_error(error):
    return render_template('pages/page-404.html'), 404


@blueprint.errorhandler(500)
def internal_error(error):
    return render_template('pages/page-500.html'), 500
