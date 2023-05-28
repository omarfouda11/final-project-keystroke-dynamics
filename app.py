import os
import json
import Levenshtein as lev
import logging
import statistics
import random
import time
import phonenumbers
from datetime import datetime
from typing import List, Dict
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, IntegerField, DateField
from wtforms.fields import DecimalField
from wtforms.validators import DataRequired , Optional, Email, ValidationError, NumberRange, Length
from stdnum.luhn import is_valid as is_creditcard_valid
from creditcard import CreditCard
from twilio.rest import Client

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s'
)
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = '1234567890123456'
app.config['SQLALCHEMY_DATABASE_URI'] = 'mssql+pyodbc://DESKTOP-MSFSRA5\\Omar@DESKTOP-MSFSRA5\\FJGVVKH/fraud_detection?driver=ODBC+Driver+18+for+SQL+Server&Trusted_Connection=yes&TrustServerCertificate=yes'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Twilio credentials
TWILIO_ACCOUNT_SID = "ACa18ac54987464782589916dab572be74"
TWILIO_AUTH_TOKEN = "5107659f6ae8146db2ad22e409a63709"
TWILIO_PHONE_NUMBER = "+16074781151"

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), nullable=False)
    credit_card_number = db.Column(db.String(20), nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    dwell_time = db.Column(db.String(1024), nullable=False)
    key_press_times = db.Column(db.UnicodeText, nullable=False)
    key_release_times = db.Column(db.UnicodeText, nullable=False)
    digraph_latencies = db.Column(db.UnicodeText, nullable=False)
    typing_speed = db.Column(db.Float, nullable=False)
    typing_errors = db.Column(db.Integer, nullable=False)
    def __repr__(self):
        return f'<User {self.name}>'
    
def is_valid_phone_number(phone_number, country):
    try:
        parsed_number = phonenumbers.parse(phone_number, country)
        if not phonenumbers.is_valid_number(parsed_number):
            return False
        return True
    except phonenumbers.NumberParseException:
        return False


def calculate_typing_speed(sentence, start_time, end_time):
    num_words = len(sentence.split())
    time_elapsed = end_time - start_time
    return num_words / time_elapsed * 60
    
class PhoneNumberValidator:
    def __init__(self, country="EG", message=None):
        self.country = country
        self.message = message

    def __call__(self, form, field):
        phone_number = field.data
        if not is_valid_phone_number(phone_number, self.country):
            message = self.message
            if message is None:
                message = f"Invalid {self.country} phone number."
            raise ValidationError(message)

class CVCLengthValidator:
    def __init__(self, length=3, message=None):
        self.length = length
        self.message = message

    def __call__(self, form, field):
        cvc = str(field.data)
        if len(cvc) != self.length:
            message = self.message
            if message is None:
                message = f"CVC must be {self.length} digits."
            raise ValidationError(message)   

class OTPForm(FlaskForm):
    otp = StringField('OTP', validators=[DataRequired(), Length(min=6, max=6)])
    submit = SubmitField('Verify')
                         
def credit_card_validator(form, field):
    card_number = field.data.replace(" ", "")
    card = CreditCard(card_number)
    if not card.is_valid:
        raise ValidationError("Invalid credit card number.")

def credit_card_expiration_validator(form, field):
    expiry_date = field.data
    if expiry_date <= datetime.now().date():
        raise ValidationError("Invalid expiry date. Please enter a valid expiry date.")    
    
class TypingDataRequired(DataRequired):
    def __call__(self, form, field):
        data = request.form.get(field.name)
        if not data:
            raise ValidationError('This field is required.')   
        
class EnrollmentForm(FlaskForm):
    name = StringField('Full Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    credit_card_number = StringField('Credit Card Number', validators=[DataRequired(), credit_card_validator])
    phone = StringField('Phone Number', validators=[DataRequired(), PhoneNumberValidator(country="EG")])
    typing_test = StringField('Type the following sentence: The quick brown fox jumps over the lazy dog', validators=[DataRequired()])
    typing_key_press_times = StringField(validators=[DataRequired()])
    typing_key_release_times = StringField(validators=[DataRequired()])
    typing_digraph_latencies = StringField(validators=[DataRequired()])
    typing_start_time = StringField(validators=[DataRequired()])
    typing_end_time = StringField(validators=[DataRequired()])
    submit = SubmitField('Submit')
    
class TransactionForm(FlaskForm):
    credit_card_number = StringField('Credit Card Number', validators=[DataRequired()])
    expiry_date = DateField('Expiry Date (MM/YYYY)')
    cvc = StringField('CVC', validators=[DataRequired(), Length(min=3, max=3, message="CVC must be exactly 3 numbers")])
    sentence = StringField('Type the following sentence: The quick brown fox jumps over the lazy dog', validators=[TypingDataRequired()])
    amount = DecimalField('Amount', validators=[DataRequired(), NumberRange(min=1, message="Amount must be greater than zero")])
    typing_key_press_times = StringField('Typing Key Press Times', validators=[DataRequired()])
    typing_key_release_times = StringField('Typing Key Release Times', validators=[DataRequired()])
    typing_digraph_latencies = StringField('Typing Digraph Latencies', validators=[DataRequired()])
    typing_start_time = StringField('Typing Start Time', validators=[TypingDataRequired()])
    typing_end_time = StringField('Typing End Time', validators=[TypingDataRequired()])
    submit = SubmitField('Submit')

def calculate_mean(data):
    if isinstance(data, dict) and 'latency' in data:
        return data['latency']
    elif isinstance(data, list):
        if all(isinstance(item, dict) and 'latency' in item for item in data):
            return sum(item['latency'] for item in data) / len(data) if len(data) > 0 else 0
        else:
            return sum(data) / len(data) if len(data) > 0 else 0
        

def calculate_standard_deviation(column):
    numerical_values = []

    if isinstance(column, list):
        if(isinstance(column[0], dict)):
            latency_values = [d['latency'] for d in column]
            numerical_values.extend(latency_values)
        else:
            numerical_values.extend(column)
    return statistics.stdev(numerical_values)


def calculate_typing_speed(sentence, start_time, end_time):
    num_words = len(sentence.split())
    time_elapsed = end_time - start_time
    return num_words / time_elapsed * 60

    
def process_key_press_data(key_press_times_list):
    import json 
    
    key_press_times_dict = {}
    for time in json.loads(key_press_times_list):
        try:
            key_code = int(time['key_code'])
            key_time = int(time['time'])
            key_press_times_dict[key_code] = key_time
        except KeyError as e:
            print(f"KeyError: {e} - Problematic dictionary: {time}")
    key_press_times = [{'key_code': key_code, 'time': time} for key_code, time in key_press_times_dict.items()]
    return key_press_times

def process_key_release_data(key_release_times_list):
    import json
    key_release_times_dict = {}
    for time in json.loads(key_release_times_list):
        try:
        
            key_code = int(time['key_code'])
            key_time = int(time['time'])
            key_release_times_dict[key_code] = key_time
        except KeyError as e:
            print(f"KeyError: {e} - Problematic dictionary: {time}")
    key_release_times = [{'key_code': key_code, 'time': time} for key_code, time in key_release_times_dict.items()]
    return key_release_times

def process_digraph_latencies(digraph_latencies_str):
    return json.loads(digraph_latencies_str)

def validate_user_data(user, credit_card_number, phone):
    return user.credit_card_number == credit_card_number and user.phone == phone

def calculate_dwell_time(key_press_times, key_release_times):
    print(len(key_press_times),len(key_release_times))
    if len(key_press_times) != len(key_release_times):
        raise ValueError("Mismatch in key_press_times and key_release_times length")

    dwell_time_list = []

    for key_press, key_release in zip(key_press_times, key_release_times):
        key_code_press = key_press['key_code']
        key_code_release = key_release['key_code']
        time_press = key_press['time']
        time_release = key_release['time']

        if key_code_press == key_code_release:
            dwell_time = time_release - time_press
            dwell_time_list.append(dwell_time)

    return dwell_time_list


def calculate_typing_errors(original_text, typed_text):
    original_words = original_text.lower().split()
    typed_words = typed_text.lower().split()
    errors = 0
    for word1, word2 in zip(original_words, typed_words):
        errors += lev.distance(word1, word2)
    if len(original_words) != len(typed_words):
        errors += abs(len(original_words) - len(typed_words))
    return errors

def euclidean_distance(x, y):
    return sum((a - b) ** 2 for a, b in zip(x, y)) ** 0.5

def euclidean_distance_dict(x, y):
    x_values = [item['latency'] for item in x]
    y_values = [item['latency'] for item in y]
    return sum((x_i - y_i) ** 2 for x_i, y_i in zip(x_values, y_values)) ** 0.5


def is_typing_pattern_different(dwell_time, user_dwell_time, digraph_latencies, user_digraph_latencies, typing_speed, user_typing_speed, typing_errors, user_typing_errors):
    user_dwell_time_list = [int(dt) for dt in json.loads(user_dwell_time)]
    dwell_time_difference = euclidean_distance(dwell_time, user_dwell_time_list)
    digraph_latencies_difference = euclidean_distance_dict(digraph_latencies, user_digraph_latencies)
    typing_speed_difference = abs(typing_speed - user_typing_speed)
    typing_errors_difference = abs(typing_errors - int(user_typing_errors))
    data = [user_dwell_time_list, user_digraph_latencies, [user_typing_speed], [int(user_typing_errors)]]
    sd = [user_dwell_time_list, user_digraph_latencies]
    print('data',data)
    mean_data = [calculate_mean(column) for column in data]
    std_data = [calculate_standard_deviation(column) for column in sd]
    differences = [dwell_time_difference, digraph_latencies_difference, typing_speed_difference, typing_errors_difference]
    normalized_differences = [abs(diff - mean) / std for diff, mean, std in zip(differences, mean_data, std_data)]
    dwell_time_weight = 0.2
    digraph_latencies_weight = 0.3
    typing_speed_weight = 0.3
    typing_errors_weight = 0.2
    weights = [dwell_time_weight, digraph_latencies_weight, typing_speed_weight, typing_errors_weight]
    
    weighted_score = sum(normalized_diff * weight for normalized_diff, weight in zip(normalized_differences, weights))
    print(weighted_score)
    return weighted_score > 3

def process_transaction(user,form,t,d):
    typed_sentence = form.sentence.data
    typing_key_press_times = request.form.get('typing_key_press_times')
    typing_key_release_times = request.form.get('typing_key_release_times')
    typing_digraph_latencies = request.form.get('typing_digraph_latencies')
    typing_start_time = float(request.form.get('typing_start_time'))
    typing_end_time = float(request.form.get('typing_end_time'))
    reference_sentence = "The quick brown fox jumps over the lazy dog"
    typing_speed = calculate_typing_speed(reference_sentence, typing_start_time, typing_end_time)
    typing_errors = calculate_typing_errors(reference_sentence, typed_sentence)
    dwell_time = calculate_dwell_time(t,d)
    digraph_latencies = process_digraph_latencies(typing_digraph_latencies)
    user_digraph_latencies = process_digraph_latencies(user.digraph_latencies)
    threshold = 0.5
    if is_typing_pattern_different(dwell_time, user.dwell_time, digraph_latencies, user_digraph_latencies, typing_speed, user.typing_speed, typing_errors, user.typing_errors):
        return {"result": False, "message": "Transaction detected as fraud"}
    else:
        return {"result": True, "message": "Transaction successful"}

def send_sms(to_phone_number):
    
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    otp = random.randint(100000, 999999)
    message_body = f"Your OTP for transaction verification is: {otp}"
    try:
        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=to_phone_number
        )
    except Exception as e:
        logging.error(f"Error sending SMS: {e}")
        flash("Error sending SMS. Please try again later.", 'danger')
        return None
    return otp

@app.route('/')
def index():
    return redirect(url_for('enroll'))

@app.route('/enroll', methods=['GET', 'POST'])
def enroll():
    form = EnrollmentForm()
    
    if form.validate_on_submit():
        
        name = form.name.data
        email = form.email.data
        credit_card_number = form.credit_card_number.data.replace(' ', '')
        phone = form.phone.data
        
        typing_key_press_times = json.loads(form.typing_key_press_times.data)
        typing_key_release_times = json.loads(form.typing_key_release_times.data)
        typing_digraph_latencies = json.loads(request.form.get('typing_digraph_latencies') or '[]')
        typing_start_time_str = form.typing_start_time.data
        typing_end_time_str = form.typing_end_time.data        
        if not (typing_key_press_times and typing_key_release_times and typing_digraph_latencies and typing_start_time_str and typing_end_time_str):
            flash('Please complete the typing test.', 'danger')
            return redirect(url_for('enroll'))
        typing_start_time = float(typing_start_time_str)
        typing_end_time = float(typing_end_time_str)
        typed_sentence = form.typing_test.data
        reference_sentence = "The quick brown fox jumps over the lazy dog"
        typing_speed = calculate_typing_speed(reference_sentence, typing_start_time, typing_end_time)
        typing_errors = calculate_typing_errors(reference_sentence, typed_sentence)
        user = User.query.filter_by(email=email).first()
        if user:
            flash('User already enrolled!', 'danger')
            return redirect(url_for('enroll'))
        
        dwell_time_list = calculate_dwell_time(typing_key_press_times, typing_key_release_times)
        dwell_time = json.dumps(dwell_time_list)  # Changed to JSON list
        print(dwell_time)
        new_user = User(name=name, email=email, credit_card_number=credit_card_number, phone=phone,
                dwell_time=dwell_time,
                key_press_times=json.dumps(typing_key_press_times),
                key_release_times=json.dumps(typing_key_release_times),
                digraph_latencies=json.dumps(typing_digraph_latencies),
                typing_speed=typing_speed,
                typing_errors=typing_errors)
        db.session.add(new_user)
        db.session.commit()
        flash('Enrollment successful!', 'success')
        return render_template('transaction.html', form=TransactionForm())
    return render_template('enroll.html', form=form)

@app.route('/transaction', methods=['GET', 'POST'])
def transaction():
    form = TransactionForm()
    if request.method == 'POST':
        
        if form.validate_on_submit():
           
            credit_card_number = form.credit_card_number.data.replace(' ', '')  # Removing any spaces
            amount = float(request.form['amount'])
            expiry_date = form.expiry_date.data
            cvc = form.cvc.data
            typing_key_press_times = json.loads(form.typing_key_press_times.data)
            typing_key_release_times = json.loads(form.typing_key_release_times.data)
            typing_digraph_latencies = json.loads(form.typing_digraph_latencies.data)
            typing_start_time = form.typing_start_time.data
            typing_end_time = form.typing_end_time.data
            user = User.query.filter_by(credit_card_number=credit_card_number).first()
            if not user:
                flash("User not found!", "danger")
                return redirect(url_for('transaction'))
            else:
                transaction_result = process_transaction(user,form,typing_key_press_times,typing_key_release_times)
                if transaction_result["result"]:
                    flash(transaction_result["message"], "success")
                else:
                    otp = send_sms(user.phone)
                    if otp:
                        session['verification_code'] = otp
                        flash(transaction_result["message"], "warning")
                        return redirect(url_for('verify_2fa'))
                    else:
                        flash("Error sending OTP. Please try again later.", 'danger')
        else:
            for _, errors in form.errors.items():
                for error in errors:
                    flash(error, "danger")
    return render_template('transaction.html', form=form)

@app.route('/verify_2fa', methods=['GET', 'POST'])
def verify_2fa():
    form = OTPForm()
    if form.validate_on_submit():
        user_code = form.otp.data.strip()  # Remove leading/trailing spaces
        stored_code = session.get('verification_code')
        
        if str(user_code) == str(stored_code):
            session.pop('verification_code', None)
            flash("Verification successful, transaction completed.", 'success')
            return redirect(url_for('transaction'))
        else:
            flash("Incorrect code, please try again.", 'danger')
    return render_template('verify_2fa.html', form=form)

if __name__ == '__main__':
    app.run(debug=True)



            






