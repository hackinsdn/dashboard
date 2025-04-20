# -*- encoding: utf-8 -*-
"""
Copyright (c) 2019 - present AppSeed.us
"""

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, DateField
from wtforms.validators import Email, DataRequired, Optional

# login and registration


class LoginForm(FlaskForm):
    identifier = StringField('Identifier',
                         id='username_login',
                         validators=[DataRequired()])
    password = PasswordField('Password',
                             id='pwd_login',
                             validators=[DataRequired()])


class CreateAccountForm(FlaskForm):
    username = StringField('Username',
                         id='username_create',
                         validators=[DataRequired()])
    email = StringField('Email',
                      id='email_create',
                      validators=[DataRequired(), Email()])
    password = PasswordField('Password',
                             id='pwd_create',
                             validators=[DataRequired()])


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
    password = PasswordField('Password',
                             id='pwd_reset',
                             validators=[DataRequired()])
    confirmation_token = StringField('Confirmation Token',
                                    id='confirmation_token',
                                    validators=[DataRequired()])
