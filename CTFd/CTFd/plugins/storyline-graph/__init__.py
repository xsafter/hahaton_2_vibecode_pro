from flask import Blueprint, render_template, request, jsonify, redirect, url_for
from CTFd.models import db, Challenges, Solves, Users, Teams
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.utils.user import get_current_user, get_current_team
from CTFd.plugins import register_plugin_assets_directory, override_template, bypass_csrf_protection
from CTFd.plugins import register_plugin_asset
from CTFd.utils import get_config
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
import json
from pathlib import Path

# Database Models
class StorylineChallenge(db.Model):
    __tablename__ = 'storyline_challenges'

    id = Column(Integer, primary_key=True)
    challenge_id = Column(Integer, ForeignKey('challenges.id'), nullable=False, unique=True)
    predecessor_id = Column(Integer, ForeignKey('challenges.id'), nullable=True)
    max_lifetime = Column(Integer, nullable=True)  # in minutes

    challenge = relationship("Challenges", foreign_keys=[challenge_id])
    predecessor = relationship("Challenges", foreign_keys=[predecessor_id])

class SolutionDescription(db.Model):
    __tablename__ = 'solution_descriptions'

    id = Column(Integer, primary_key=True)
    solve_id = Column(Integer, ForeignKey('solves.id'), nullable=False)
    team_id = Column(Integer, ForeignKey('teams.id'), nullable=False)
    challenge_id = Column(Integer, ForeignKey('challenges.id'), nullable=False)
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    solve = relationship("Solves")
    team = relationship("Teams")
    challenge = relationship("Challenges")

# Helper functions
def get_unlocked_challenges_for_team(team_id):
    """Get all challenges that are currently unlocked for a team"""
    if not team_id:
        return []

    # Get all solved challenges by this team
    solved_challenges = db.session.query(Solves.challenge_id, Solves.date).filter_by(team_id=team_id).all()
    solved_ids = [solve.challenge_id for solve in solved_challenges]
    solved_dict = {solve.challenge_id: solve.date for solve in solved_challenges}

    # Get all storyline challenges
    storyline_challenges = StorylineChallenge.query.all()
    storyline_dict = {sc.challenge_id: sc for sc in storyline_challenges}

    unlocked_ids = set()

    # Check each challenge
    for challenge in Challenges.query.all():
        if challenge.id in storyline_dict:
            sc = storyline_dict[challenge.id]

            # If no predecessor, it's a root challenge (always unlocked)
            if not sc.predecessor_id:
                unlocked_ids.add(challenge.id)
            # If predecessor is solved, check if this challenge is unlocked
            elif sc.predecessor_id in solved_ids:
                predecessor_solve_time = solved_dict[sc.predecessor_id]

                # If there's a time limit, check if it's still valid
                if sc.max_lifetime:
                    time_limit = timedelta(minutes=sc.max_lifetime)
                    if datetime.utcnow() - predecessor_solve_time <= time_limit:
                        unlocked_ids.add(challenge.id)
                else:
                    # No time limit, so it's unlocked
                    unlocked_ids.add(challenge.id)
        else:
            # Non-storyline challenges are always unlocked
            unlocked_ids.add(challenge.id)

    return list(unlocked_ids)

def get_graph_data(team_id=None):
    """Get graph data for visualization"""
    challenges = Challenges.query.all()
    storyline_challenges = StorylineChallenge.query.all()

    # Create lookup dictionaries
    storyline_dict = {sc.challenge_id: sc for sc in storyline_challenges}

    nodes = []
    edges = []

    # Get solved challenges for team if specified
    solved_ids = set()
    if team_id:
        solved_challenges = Solves.query.filter_by(team_id=team_id).all()
        solved_ids = {solve.challenge_id for solve in solved_challenges}
        unlocked_ids = set(get_unlocked_challenges_for_team(team_id))

    for challenge in challenges:
        status = 'locked'
        if team_id:
            if challenge.id in solved_ids:
                status = 'solved'
            elif challenge.id in unlocked_ids:
                status = 'unlocked'
        else:
            # Admin view - show all as unlocked
            status = 'unlocked'

        node = {
            'id': challenge.id,
            'label': challenge.name,
            'status': status,
            'category': challenge.category,
            'value': challenge.value
        }

        if challenge.id in storyline_dict:
            sc = storyline_dict[challenge.id]
            if sc.max_lifetime:
                node['max_lifetime'] = sc.max_lifetime

        nodes.append(node)

    # Create edges
    for sc in storyline_challenges:
        if sc.predecessor_id:
            edges.append({
                'from': sc.predecessor_id,
                'to': sc.challenge_id,
                'has_timer': sc.max_lifetime is not None
            })

    return {'nodes': nodes, 'edges': edges}

# Plugin Blueprint
storyline_bp = Blueprint('storyline', __name__, template_folder='templates', static_folder='assets')

@storyline_bp.route('/admin/storyline-graph')
@admins_only
def admin_graph():
    """Admin view of the storyline graph"""
    graph_data = get_graph_data()
    return render_template('admin_graph.html', graph_data=json.dumps(graph_data))

@storyline_bp.route('/admin/storyline-manage')
@admins_only
def admin_storyline_manage():
    """Admin interface for managing storyline challenges"""
    return render_template('admin_storyline.html')

@storyline_bp.route('/storyline-graph')
@authed_only
def player_graph():
    """Player view of the storyline graph"""
    team = get_current_team()
    team_id = team.id if team else None
    graph_data = get_graph_data(team_id)
    return render_template('player_graph.html', graph_data=json.dumps(graph_data))

@storyline_bp.route('/api/storyline/graph')
@authed_only
def api_graph():
    """API endpoint for graph data"""
    team = get_current_team()
    team_id = team.id if team else None
    graph_data = get_graph_data(team_id)
    return jsonify(graph_data)

@storyline_bp.route('/api/admin/storyline/graph')
@admins_only
def api_admin_graph():
    """Admin API endpoint for graph data"""
    graph_data = get_graph_data()
    return jsonify(graph_data)

@storyline_bp.route('/api/admin/storyline/challenge/<int:challenge_id>', methods=['POST'])
@admins_only
@bypass_csrf_protection
def update_storyline_challenge(challenge_id):
    """Update or create storyline challenge settings"""
    data = request.get_json()

    # Find or create storyline challenge
    sc = StorylineChallenge.query.filter_by(challenge_id=challenge_id).first()
    if not sc:
        sc = StorylineChallenge(challenge_id=challenge_id)
        db.session.add(sc)

    # Update fields
    predecessor_id = data.get('predecessor_id')
    sc.predecessor_id = predecessor_id if predecessor_id else None

    max_lifetime = data.get('max_lifetime')
    sc.max_lifetime = max_lifetime if max_lifetime else None

    db.session.commit()

    return jsonify({'success': True})

@storyline_bp.route('/api/admin/storyline/challenges')
@admins_only
def api_admin_storyline_challenges():
    """Get all storyline challenge configurations for admin interface"""
    storyline_challenges = StorylineChallenge.query.all()
    result = {}
    for sc in storyline_challenges:
        result[sc.challenge_id] = {
            'predecessor_id': sc.predecessor_id,
            'max_lifetime': sc.max_lifetime
        }
    return jsonify(result)

@storyline_bp.route('/api/storyline/solution-description', methods=['POST'])
@authed_only
def submit_solution_description():
    """Submit solution description after solving a challenge"""
    data = request.get_json()
    team = get_current_team()

    if not team:
        return jsonify({'error': 'No team found'}), 400

    challenge_id = data.get('challenge_id')
    description = data.get('description', '').strip()

    if not description:
        return jsonify({'error': 'Description is required'}), 400

    # Find the solve record
    solve = Solves.query.filter_by(
        team_id=team.id,
        challenge_id=challenge_id
    ).order_by(Solves.date.desc()).first()

    if not solve:
        return jsonify({'error': 'No solve found for this challenge'}), 400

    # Check if description already exists
    existing = SolutionDescription.query.filter_by(
        solve_id=solve.id,
        team_id=team.id,
        challenge_id=challenge_id
    ).first()

    if existing:
        existing.description = description
    else:
        solution_desc = SolutionDescription(
            solve_id=solve.id,
            team_id=team.id,
            challenge_id=challenge_id,
            description=description
        )
        db.session.add(solution_desc)

    db.session.commit()
    return jsonify({'success': True})

def load(app):
    """Plugin entry point"""

    # Create database tables
    with app.app_context():
        db.create_all()

    # Register blueprint
    app.register_blueprint(storyline_bp)

    # Register assets
    dir_path = Path(__file__).parent
    register_plugin_assets_directory(
        app,
        base_path=f'/plugins/storyline-graph/assets/',
#        directory=str(dir_path / 'assets')
    )

    # Override challenge creation/edit template to add storyline fields
    template_path = dir_path / 'templates' / 'challenge_form_override.html'
    if template_path.exists():
        override_template('admin/challenges.html', open(template_path).read())

    # Add custom JavaScript to challenge pages for solution descriptions
    js_path = dir_path / 'assets' / 'storyline.js'
    if js_path.exists():
        register_plugin_asset(app, asset_path='/plugins/storyline-graph/assets/storyline.js')

    # Override challenges view for players to respect storyline unlocking
    def custom_challenges_view():
        from flask import render_template
        from CTFd.utils.user import get_current_user
        from CTFd.utils.modes import get_model

        user = get_current_user()
        team = get_current_team()

        if team:
            unlocked_challenge_ids = get_unlocked_challenges_for_team(team.id)
            challenges = Challenges.query.filter(Challenges.id.in_(unlocked_challenge_ids)).all()
        else:
            challenges = Challenges.query.all()

        return render_template('challenges.html', challenges=challenges)

    # Don't override challenges view for now - let's keep it simple for MVP
    # app.view_functions['challenges.challenges_view'] = custom_challenges_view
