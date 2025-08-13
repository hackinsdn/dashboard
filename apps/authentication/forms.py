# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, DateField, BooleanField
from wtforms.validators import Email, DataRequired, Optional, Length, EqualTo, Regexp

# login and registration


class LoginForm(FlaskForm):
    identifier = StringField('Identifier',
                         id='username_login',
                         validators=[DataRequired()])
    password = PasswordField('Password',
                             id='pwd_login',
                             validators=[DataRequired()])


class CreateAccountForm(FlaskForm):
    username = StringField(
        'Username',
        id='username_create',
        # Trim leading/trailing spaces before validating
        filters=[lambda x: x.strip() if isinstance(x, str) else x],
        validators=[
            DataRequired(),
            Length(min=3, max=20),
            # Issue baseline: allow only A–Z a–z 0–9 and hyphen (-)
            Regexp(
                r'^[A-Za-z0-9-]+$',
                message='Use only letters, numbers, and hyphen (-).'
            ),
        ],
    )
    email = StringField('Email',
                      id='email_create',
                      validators=[DataRequired(), Email()])
    password = PasswordField('Password',
                             id='pwd_create',
                             validators=[DataRequired()])

# Terms and conditions
    terms = BooleanField(
        'I agree to the terms',
        id='agreeTerms',
        validators=[DataRequired(message='You must agree to the terms.')]
    )

class GroupForm(FlaskForm):
    groupname = StringField('Group Name', 
                            id='groupname', 
                            validators=[DataRequired()])
    description = StringField('Description', id='description', validators=[Optional()])
    organization = StringField('Organization', id='organization', validators=[Optional()])
    expiration = DateField("Expiration", format='%Y-%m-%d', validators=[Optional()])
    accesstoken = StringField('Accesstoken', id='accesstoken', validators=[DataRequired()])


class ConfirmAccountForm(FlaskForm):
    confirmation_token = StringField('Confirmation Token',
                                    id='confirmation_token',
                                    validators=[DataRequired()])

class ResetPasswordForm(FlaskForm):
    identifier = StringField('Identifier',
                      id='identifier',
                      validators=[DataRequired()])

class ResetPasswordConfirmForm(FlaskForm):
    password = PasswordField(
        'Password',
        id='pwd_reset',
        validators=[
            DataRequired(),
            Length(min=8),
            EqualTo('password_confirm', message='Passwords must match'),
        ],
    )
    password_confirm = PasswordField(
        'Password confirm',
        validators=[
            DataRequired(),
            Length(min=8),
        ],
    )
