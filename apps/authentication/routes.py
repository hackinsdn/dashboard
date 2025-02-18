# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

from flask import render_template, redirect, request, url_for
from flask import current_app as app
from flask_login import (
    current_user,
    login_user,
    logout_user
)

from apps import db, login_manager, oauth
from apps.config import app_config
from apps.audit_mixin import get_remote_addr
from apps.authentication import blueprint
from apps.authentication.forms import LoginForm, CreateAccountForm, GroupForm
from apps.authentication.models import Users, LoginLogging, Groups

from apps.authentication.util import verify_pass

@blueprint.route('/')
def route_default():
    return redirect(url_for('authentication_blueprint.login'))


# Login & Registration

@blueprint.route('/login/', methods=['GET', 'POST'])
def login():
    login_form = LoginForm(request.form)
    if 'login' in request.form:

        # read form data
        username = request.form['username']
        password = request.form['password']

        # Locate user
        user = Users.query.filter_by(username=username).first()

        # Check the password
        if user and verify_pass(password, user.password):

            login_user(user)
            app.logger.info(f"Successful login ipaddr={get_remote_addr()} login={username} auth_provider=local")
            login_log = LoginLogging(ipaddr=get_remote_addr(), login=username, auth_provider="local", success=True)
            db.session.add(login_log)
            db.session.commit()
            return redirect(url_for('authentication_blueprint.route_default'))

        app.logger.warn(f"Failed login ipaddr={get_remote_addr()} login={username} auth_provider=local")
        login_log = LoginLogging(ipaddr=get_remote_addr(), login=username, auth_provider="local", success=False)
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

    app.logger.info(f"Successful login ipaddr={get_remote_addr()} login={subject} auth_provider={issuer} email={email}")
    login_log = LoginLogging(ipaddr=get_remote_addr(), login=subject, auth_provider=issuer, success=True)
    db.session.add(login_log)
    db.session.commit()

    login_user(user)
    return redirect(url_for('authentication_blueprint.route_default'))


@blueprint.route('/register', methods=['GET', 'POST'])
def register():
    create_account_form = CreateAccountForm(request.form)
    if 'register' in request.form:

        username = request.form['username']
        email = request.form['email']

        # Check usename exists
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

        # else we can create the user
        user = Users(**request.form)
        db.session.add(user)
        db.session.commit()

        return render_template('pages/register.html',
                               msg='User created please <a href="/login">login</a>',
                               success=True,
                               form=create_account_form)

    else:
        return render_template('pages/register.html', form=create_account_form)

@blueprint.route('/groups/create', methods=['GET', 'POST'])
def create_group():
    form = GroupForm()
    students = Users.query.filter_by(category='student').all()
    
    if form.validate_on_submit():


        groupname = form.groupname.data
        description = form.description.data
        organization = form.organization.data
        expiration = form.expiration.data  
        accesstoken = form.accesstoken.data
        
        new_group = Groups(
        groupname=groupname,
        description=description,
        organization=organization,
        expiration=expiration,
        accesstoken=accesstoken
    )
        db.session.add(new_group)
        
        db.session.commit()
        
        return redirect(url_for('home_blueprint.view_group'))

    return render_template('pages/create_group.html', form=form, students=students)

@blueprint.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('authentication_blueprint.login'))


# Errors

@login_manager.unauthorized_handler
def unauthorized_handler():
    return render_template('pages/page-403.html'), 403


@blueprint.errorhandler(403)
def access_forbidden(error):
    return render_template('pages/page-403.html'), 403


@blueprint.errorhandler(404)
def not_found_error(error):
    return render_template('pages/page-404.html'), 404


@blueprint.errorhandler(500)
def internal_error(error):
    return render_template('pages/page-500.html'), 500