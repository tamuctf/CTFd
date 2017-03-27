import logging
import re
import time
import json
import collections

from flask import render_template, request, redirect, jsonify, url_for, session, Blueprint

from CTFd.utils import ctftime, view_after_ctf, authed, unix_time, get_kpm, \
    user_can_view_challenges, is_admin, get_config, get_ip, is_verified, \
    ctf_started, ctf_ended, ctf_name, get_instance_static,\
    get_instance_dynamic, update_generated_files

from CTFd.models import db, Challenges, Files, Solves, WrongKeys, Tags, Teams, Awards

from sqlalchemy.sql import or_

from jinja2 import Template

challenges = Blueprint('challenges', __name__)


@challenges.route('/challenges', methods=['GET'])
def challenges_view():
    errors = []
    start = get_config('start') or 0
    end = get_config('end') or 0
    if not is_admin():  # User is not an admin
        if not ctftime():
            # It is not CTF time
            if view_after_ctf():  # But we are allowed to view after the CTF ends
                pass
            else:  # We are NOT allowed to view after the CTF ends
                if get_config('start') and not ctf_started():
                    errors.append('{} has not started yet'.format(ctf_name()))
                if (get_config('end') and ctf_ended()) and not view_after_ctf():
                    errors.append('{} has ended'.format(ctf_name()))
                return render_template('chals.html', errors=errors, start=int(start), end=int(end))
        if get_config('verify_emails') and not is_verified():  # User is not confirmed
            return redirect(url_for('auth.confirm_user'))
    if user_can_view_challenges():  # Do we allow unauthenticated users?
        if get_config('start') and not ctf_started():
            errors.append('{} has not started yet'.format(ctf_name()))
        if (get_config('end') and ctf_ended()) and not view_after_ctf():
            errors.append('{} has ended'.format(ctf_name()))
        return render_template('chals.html', errors=errors, start=int(start), end=int(end))
    else:
        return redirect(url_for('auth.login', next='challenges'))


@challenges.route('/chals', methods=['GET'])
def chals():
    if not is_admin():
        if not ctftime():
            if view_after_ctf():
                pass
            else:
                return redirect(url_for('views.static_html'))
    if user_can_view_challenges() and (ctf_started() or is_admin()):
        columns = ('id', 'name', 'value', 'description', 'category',
                   'instanced', 'generated', 'generator')
        hidden_flt = or_(Challenges.hidden is not True, Challenges.hidden is None)
        chals = Challenges.query.filter(hidden_flt).add_columns(*columns)
        chals = chals.order_by(Challenges.value).all()

        game = []
        instance_log = logging.getLogger('instancing')

        for chal in chals:

            tags_query = Tags.query.add_columns('tag').filter_by(chal=chal.name)
            tags = [tag.tag for tag in tags_query.all()]

            name = chal.name
            desc = chal.description

            if chal.instanced:
                try:
                    if chal.generated:
                        params, files = get_instance_dynamic(chal.generator)
                        if files:
                            update_generated_files(chal.id, files)

                        # Query the DB which now include newly added files and static files
                        files_query = Files.query.filter_by(chal=chal.id)
                        files = [str(f.location) for f in files_query.all()]
                    else:
                        params, files = get_instance_static(chal.id)

                    assert isinstance(params, collections.Mapping)
                    assert isinstance(files, collections.Iterable)
                except:
                    instance_log.exception("instancing error while generating "
                                           "chal list in challenge #{0.id} "
                                           "({0.name})".format(chal))
                    continue

                name = Template(chal.name).render(params)
                desc = Template(chal.description).render(params)

            else:
                files_query = Files.query.filter_by(chal=chal.id)
                files = [str(f.location) for f in files_query.all()]

            game.append({'id': chal.id, 'name': name, 'tags': tags,
                         'description': desc, 'value': chal.value,
                         'files': files, 'category': chal.category})

        db.session.close()
        return jsonify({'game': game})
    else:
        db.session.close()
        return redirect(url_for('auth.login', next='chals'))


@challenges.route('/chals/solves')
def solves_per_chal():
    if not user_can_view_challenges():
        return redirect(url_for('auth.login', next=request.path))
    solves_counter = db.func.count(Solves.chalid).label('solves')
    solves_sub = db.session.query(Solves.chalid, solves_counter) \
                           .join(Teams, Solves.teamid == Teams.id) \
                           .filter(not Teams.banned) \
                           .group_by(Solves.chalid).subquery()

    solves = db.session.query(solves_sub.columns.chalid, solves_sub.columns.solves, Challenges.name) \
                       .join(Challenges, solves_sub.columns.chalid == Challenges.id).all()
    json = {}
    for chal, count, name in solves:
        json[chal] = count
    db.session.close()
    return jsonify(json)


@challenges.route('/solves')
@challenges.route('/solves/<int:teamid>')
def solves(teamid=None):
    solves = None
    awards = None
    if teamid is None:
        if is_admin():
            solves = Solves.query.filter_by(teamid=session['id']).all()
        elif user_can_view_challenges():
            solves = Solves.query.join(Teams, Solves.teamid == Teams.id) \
                           .filter(Solves.teamid == session['id'], not Teams.banned).all()
        else:
            return redirect(url_for('auth.login', next='solves'))
    else:
        solves = Solves.query.filter_by(teamid=teamid).all()
        awards = Awards.query.filter_by(teamid=teamid).all()
    db.session.close()
    json = {'solves': []}
    for solve in solves:
        json['solves'].append({
            'chal': solve.chal.name,
            'chalid': solve.chalid,
            'team': solve.teamid,
            'value': solve.chal.value,
            'category': solve.chal.category,
            'time': unix_time(solve.date)
        })
    if awards:
        for award in awards:
            json['solves'].append({
                'chal': award.name,
                'chalid': None,
                'team': award.teamid,
                'value': award.value,
                'category': award.category,
                'time': unix_time(award.date)
            })
    json['solves'].sort(key=lambda k: k['time'])
    return jsonify(json)


@challenges.route('/maxattempts')
def attempts():
    if not user_can_view_challenges():
        return redirect(url_for('auth.login', next=request.path))
    chals = Challenges.query.add_columns('id').all()
    json = {'maxattempts': []}
    for chal, chalid in chals:
        fails = WrongKeys.query.filter_by(teamid=session['id'], chalid=chalid).count()
        if fails >= int(get_config("max_tries")) and int(get_config("max_tries")) > 0:
            json['maxattempts'].append({'chalid': chalid})
    return jsonify(json)


@challenges.route('/fails/<int:teamid>', methods=['GET'])
def fails(teamid):
    fails = WrongKeys.query.filter_by(teamid=teamid).count()
    solves = Solves.query.filter_by(teamid=teamid).count()
    db.session.close()
    json = {'fails': str(fails), 'solves': str(solves)}
    return jsonify(json)


@challenges.route('/chal/<int:chalid>/solves', methods=['GET'])
def who_solved(chalid):
    if not user_can_view_challenges():
        return redirect(url_for('auth.login', next=request.path))
    solves = Solves.query.join(Teams, Solves.teamid == Teams.id) \
                   .filter(Solves.chalid == chalid, not Teams.banned) \
                   .order_by(Solves.date.asc())
    json = {'teams': []}
    for solve in solves:
        json['teams'].append({'id': solve.team.id, 'name': solve.team.name, 'date': solve.date})
    return jsonify(json)


@challenges.route('/chal/<int:chalid>', methods=['POST'])
def chal(chalid):
    if ctf_ended() and not view_after_ctf():
        return redirect(url_for('challenges.challenges_view'))
    if not user_can_view_challenges():
        return redirect(url_for('auth.login', next=request.path))
    if authed() and is_verified() and (ctf_started() or view_after_ctf()):
        fails = WrongKeys.query.filter_by(teamid=session['id'], chalid=chalid).count()
        logger = logging.getLogger('keys')
        instance_log = logging.getLogger('instancing')
        data = (time.strftime("%m/%d/%Y %X"), session['username'].encode('utf-8'), request.form['key'].encode('utf-8'), get_kpm(session['id']))
        print("[{0}] {1} submitted {2} with kpm {3}".format(*data))

        # Anti-bruteforce / submitting keys too quickly
        if get_kpm(session['id']) > 10:
            if ctftime():
                wrong = WrongKeys(session['id'], chalid, request.form['key'])
                db.session.add(wrong)
                db.session.commit()
                db.session.close()
            logger.warn("[{0}] {1} submitted {2} with kpm {3} [TOO FAST]".format(*data))
            # return '3' # Submitting too fast
            return jsonify({'status': '3', 'message': "You're submitting keys too fast. Slow down."})

        solves = Solves.query.filter_by(teamid=session['id'], chalid=chalid).first()

        # Challange not solved yet
        if not solves:
            chal = Challenges.query.filter_by(id=chalid).first_or_404()
            key = unicode(request.form['key'].strip().lower())
            keys = json.loads(chal.flags)

            # Hit max attempts
            max_tries = int(get_config("max_tries"))
            if fails >= max_tries > 0:
                return jsonify({
                    'status': '0',
                    'message': "You have 0 tries remaining"
                })

            if chal.instanced:
                try:
                    if chal.generated:
                        params, files = get_instance_dynamic(chal.generator)
                    else:
                        params, files = get_instance_static(chal.id)
                    assert isinstance(params, collections.Mapping)
                    assert isinstance(files, collections.Iterable)
                except Exception:
                    instance_log.exception("instancing error during key "
                                           "submission in challenge #{0.id} "
                                           "({0.name})".format(chal))
                    logger.exception("[{0}] {1} submitted {2} with kpm {3}"
                                     " [INSTANCE_ERROR]".format(*data))
                    return '-1'

            for x in keys:
                if chal.instanced:
                    rendered_flag = Template(x['flag']).render(params)
                    print "Key template '{}' render to '{}'".format(x['flag'], rendered_flag)
                    x['flag'] = rendered_flag

                if x['type'] == 0:  # static key
                    # A consequence of the line below is that the flag must not be empty string.
                    # If this is the case, the problem is unsolvable
                    if x['flag'] and x['flag'].strip().lower() == key.strip().lower():
                        if ctftime():
                            solve = Solves(chalid=chalid, teamid=session['id'], ip=get_ip(), flag=key)
                            db.session.add(solve)
                            db.session.commit()
                            db.session.close()
                        logger.info("[{0}] {1} submitted {2} with kpm {3} [CORRECT]".format(*data))
                        # return '1' # key was correct
                        return jsonify({'status': '1', 'message': 'Correct'})
                elif x['type'] == 1:  # regex
                    res = re.match(x['flag'], key, re.IGNORECASE)
                    if res and res.group() == key:
                        if ctftime():
                            solve = Solves(chalid=chalid, teamid=session['id'], ip=get_ip(), flag=key)
                            db.session.add(solve)
                            db.session.commit()
                            db.session.close()
                        logger.info("[{0}] {1} submitted {2} with kpm {3} [CORRECT]".format(*data))
                        # return '1' # key was correct
                        return jsonify({'status': '1', 'message': 'Correct'})

            if ctftime():
                wrong = WrongKeys(session['id'], chalid, request.form['key'])
                db.session.add(wrong)
                db.session.commit()
                db.session.close()
            logger.info("[{0}] {1} submitted {2} with kpm {3} [WRONG]".format(*data))
            # return '0' # key was wrong
            if max_tries:
                attempts_left = max_tries - fails
                tries_str = 'tries'
                if attempts_left == 1:
                    tries_str = 'try'
                return jsonify({'status': '0', 'message': 'Incorrect. You have {} {} remaining.'.format(attempts_left, tries_str)})
            else:
                return jsonify({'status': '0', 'message': 'Incorrect'})

        # Challenge already solved
        else:
            logger.info("{0} submitted {1} with kpm {2} [ALREADY SOLVED]".format(*data))
            # return '2' # challenge was already solved
            return jsonify({'status': '2', 'message': 'You already solved this'})
    else:
        return '-1'
