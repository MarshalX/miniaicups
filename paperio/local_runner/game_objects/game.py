import os
import asyncio
import json
import copy
import gzip
import random

from clients import RewindClient
from game_objects.scene import Scene
from helpers import is_intersect
from constants import WIDTH, WINDOW_HEIGHT, WINDOW_WIDTH, PLAYER_COLORS, MAX_TICK_COUNT, BONUS_CHANCE, \
    BONUSES_MAX_COUNT, X_CELLS_COUNT, Y_CELLS_COUNT, SPEED, NEUTRAL_TERRITORY_SCORE, ENEMY_TERRITORY_SCORE, \
    LINE_KILL_SCORE, SAW_KILL_SCORE, AVAILABLE_BONUSES, SAW_SCORE, SAVE
from game_objects.player import Player
from game_objects.bonuses import Nitro, Slowdown, Bonus, Saw


class Game:
    available_bonuses = [b for b in [Nitro, Slowdown, Saw] if b.visio_name in AVAILABLE_BONUSES]

    RESULT_LOCATION = os.environ.get('GAME_LOG_LOCATION', './result')

    BASE_DIR = os.path.dirname(RESULT_LOCATION)

    VISIO_LOCATION = os.path.join(BASE_DIR, 'visio.gz')
    SCORES_LOCATION = os.path.join(BASE_DIR, 'scores.json')
    DEBUG_LOCATION = os.path.join(BASE_DIR, '{}')

    def get_busy_points(self):
        players_points = {(p.x, p.y) for p in self.players}
        bonuses_points = {(b.x, b.y) for b in self.bonuses}
        lines_poins = set()
        for player in self.players:
            lines_poins |= {i for i in player.lines}

        return players_points | bonuses_points | lines_poins

    def generate_bonus(self):
        if len(self.available_bonuses) > 0:
            if random.randint(1, BONUS_CHANCE) == 1 and len(self.bonuses) < BONUSES_MAX_COUNT:
                coors = Bonus.generate_coordinates(self.players, self.get_busy_points())
                bonus = random.choice(self.available_bonuses)(coors)
                self.bonuses.append(bonus)

    def get_coordinates(self, clients_count):
        dx = round(X_CELLS_COUNT / 6) * WIDTH
        dy = round(Y_CELLS_COUNT / 6) * WIDTH

        if clients_count == 1:
            coors = [(3 * dx, 3 * dy)]
        elif clients_count == 2:
            coors = [
                (2 * dx, 3 * dy),
                (4 * dx, 3 * dy),
            ]
        elif clients_count <= 4:
            coors = [
                (2 * dx, 2 * dy),
                (2 * dx, 4 * dy),
                (4 * dx, 2 * dy),
                (4 * dx, 4 * dy),
            ]
        else:
            x = round(X_CELLS_COUNT / 5) * WIDTH
            y = (WINDOW_HEIGHT + WINDOW_WIDTH - 4 * x) / 3
            b = (WINDOW_WIDTH - 2 * x) / 2
            a = y - b

            coors = [
                (x, x + a),
                (x, x + a + y  + WIDTH),

                (round(WINDOW_WIDTH / 2), WINDOW_HEIGHT - x + WIDTH),
                (round(WINDOW_WIDTH / 2), x),

                (WINDOW_WIDTH - x + WIDTH, x + a),
                (WINDOW_WIDTH - x + WIDTH, x + a + y  + WIDTH),
            ]

        coors = [(round(x / WIDTH) * WIDTH - round(WIDTH / 2),  round(y / WIDTH) * WIDTH  - round(WIDTH / 2))  for x, y in coors]
        yield from coors

    def __init__(self, clients):
        players = []
        coordinates = self.get_coordinates(len(clients))
        for index, client in enumerate(clients):
            players.append(Player(index + 1, *next(coordinates), 'Player {}'.format(index + 1), PLAYER_COLORS[index], client))

        self.players = players
        self.losers = []
        self.bonuses = []
        self.game_log = []
        self.events = []
        self.tick = 1

    def append_event(self, event, p1, p2=None):
        row = {
            'tick_num': self.tick,
            'event': event,
            'player': p1.get_state_for_event(),
        }
        if p2:
            row['other'] = p2.get_state_for_event()
        self.events.append(row)

    def check_loss(self, player, players):
        is_loss = False

        if player.y < 0 + round(WIDTH / 2):
            is_loss = True
            self.append_event('faced the border', player)

        if player.y > WINDOW_HEIGHT - round(WIDTH / 2):
            is_loss = True
            self.append_event('faced the border', player)

        if player.x < 0 + round(WIDTH / 2):
            is_loss = True
            self.append_event('faced the border', player)

        if player.x > WINDOW_WIDTH - round(WIDTH / 2):
            is_loss = True
            self.append_event('faced the border', player)

        for p in players:
            if (p.x, p.y) in player.lines[:-1]:
                if p != player:
                    p.tick_score += LINE_KILL_SCORE
                is_loss = True
                self.append_event('line crossed by other player', player, p)

        for p in players:
            if is_intersect((player.x, player.y), (p.x, p.y)) and p != player:
                if len(player.lines) >= len(p.lines):
                    is_loss = True
                    self.append_event('faced with other player', player, p)

        if len(player.territory.points) == 0:
            is_loss = True
            self.append_event('has no territory', player)

        return is_loss

    def send_game_start(self):
        start_message = {
            'x_cells_count': X_CELLS_COUNT,
            'y_cells_count': Y_CELLS_COUNT,
            'speed': SPEED,
            'width': WIDTH
        }
        self.game_log.append({'type': 'start_game', **start_message})
        for player in self.players:
            player.send_message('start_game', start_message)

    def send_game_end(self):
        self.game_log.append({
            'type': 'end_game',
            'events': self.events
        })
        for player in self.players:
            player.send_message('end_game', {})

    def send_game_tick(self):
        self.game_log.append({
            'type': 'tick',
            'players': self.get_players_states(),
            'bonuses': self.get_bonuses_states(),
            'tick_num': self.tick,
            'saw': Saw.log
        })

        for player in self.players:
            if (player.x - round(WIDTH / 2)) % WIDTH == 0 and (player.y - round(WIDTH / 2)) % WIDTH == 0:
                player.send_message('tick', {
                    'players': self.get_players_states(player),
                    'bonuses': self.get_bonuses_states(),
                    'tick_num': self.tick,
                })

        Saw.log = []

    async def game_loop_wrapper(self, *args, **kwargs):
        self.send_game_start()
        while True:
            is_game_over = await self.game_loop(*args, **kwargs)
            if is_game_over or self.tick >= MAX_TICK_COUNT:
                self.send_game_end()
                self.game_save()
                break

    def get_players_states(self, player=None):
        states = {p.id: p.get_state() for p in self.players}

        if player:
            states['i'] = states.pop(player.id)

        return states

    def get_bonuses_states(self):
        return [b.get_state() for b in self.bonuses]

    def collision_resolution(self, players_to_captured):
        p_to_c = {p: c for p, c in players_to_captured.items() if not p.is_ate(players_to_captured)[0]}
        res = {p: copy.copy(c) for p, c in p_to_c.items()}
        for p1, captured1 in p_to_c.items():
            for p2, captured2 in p_to_c.items():
                if p1 != p2:
                    res[p1].difference_update(captured2)
        return res

    async def get_command_wrapper(self, player):
        command = await player.get_command(self.tick)
        if command:
            player.change_direction(command)

    async def game_loop(self, *args, **kwargs):
        self.send_game_tick()

        futures = []
        for player in self.players:
            if (player.x - round(WIDTH / 2)) % WIDTH == 0 and (player.y - round(WIDTH / 2)) % WIDTH == 0:
                futures.append(asyncio.ensure_future(self.get_command_wrapper(player)))
        if futures:
            await asyncio.wait(futures)

        for player in self.players:
            player.move()

        players_to_captured = {}
        for player in self.players:
            player.remove_saw_bonus()

            if (player.x - round(WIDTH / 2)) % WIDTH == 0 and (player.y - round(WIDTH / 2)) % WIDTH == 0:
                player.update_lines()

                captured = player.territory.capture(player.lines)
                players_to_captured[player] = captured
                if len(captured) > 0:
                    player.lines.clear()
                    player.tick_score += NEUTRAL_TERRITORY_SCORE * len(captured)

        for player in self.players:
            is_loss = self.check_loss(player, self.players)
            if is_loss:
                self.losers.append(player)

        players_to_captured = self.collision_resolution(players_to_captured)

        for player in self.players:
            is_loss, p = player.is_ate(players_to_captured)
            if is_loss:
                self.append_event('eaten by other player', player, p)
                self.losers.append(player)

        for player in self.players:
            if (player.x - round(WIDTH / 2)) % WIDTH == 0 and (player.y - round(WIDTH / 2)) % WIDTH == 0:
                captured = players_to_captured.get(player, set())

                player.tick_action()

                for bonus in self.bonuses[:]:
                    if bonus.is_ate(player, captured):
                        bonus.apply(player)
                        self.bonuses.remove(bonus)

                        if isinstance(bonus, Saw):
                            line = player.get_direction_line()
                            Saw.append_line(line)
                            for p in self.players:
                                if p != player:
                                    if any([is_intersect((p.x, p.y), point) for point in line]):
                                        self.losers.append(p)
                                        self.append_event('killed by saw', p, player)
                                        Saw.log.append({
                                            'player': player.id,
                                            'loser': p.id,
                                            'killed': True
                                        })
                                        player.tick_score += SAW_KILL_SCORE
                                    else:
                                        removed = p.territory.split(line, player.direction, p)
                                        if len(removed) > 0:
                                            player.tick_score += SAW_SCORE
                                            Saw.append_territory(removed, p.territory.color)
                                            Saw.log.append({
                                                'player': player.id,
                                                'loser': p.id,
                                                'points': removed,
                                                'killed': False
                                            })
                if captured:
                    player.territory.points.update(captured)
                    for p in self.players:
                        if p != player:
                            removed = p.territory.remove_points(captured)
                            player.tick_score += (ENEMY_TERRITORY_SCORE - NEUTRAL_TERRITORY_SCORE) * len(removed)

        for player in self.losers:
            if player in self.players:
                self.players.remove(player)

        for player in self.players:
            player.score += player.tick_score
            player.tick_score = 0

        self.generate_bonus()

        self.tick += 1
        return len(self.players) == 0

    def save_scores(self):
        d = {p.client.get_solution_id(): p.score for p in self.losers + self.players}

        with open(self.SCORES_LOCATION, 'w') as f:
            f.write(json.dumps(d))

        return {
            "filename": os.path.basename(self.SCORES_LOCATION),
            "location": self.SCORES_LOCATION,
            "is_private": False
        }

    def get_players_external_id(self):
        return {p.id: p.client.get_solution_id() for p in self.losers + self.players}

    def save_visio(self):
        d = {
            'config': self.get_players_external_id(),
            'visio_info': self.game_log
        }
        with gzip.open(self.VISIO_LOCATION, 'wb') as f:
            f.write(json.dumps(d).encode())
        return {
            "filename": os.path.basename(self.VISIO_LOCATION),
            "location": self.VISIO_LOCATION,
            "is_private": False
        }

    def save_debug(self):
        return [
            p.save_log(self.DEBUG_LOCATION) for p in self.losers + self.players
        ]

    def game_save(self):
        result = {
            "scores": self.save_scores(),
            "debug": self.save_debug(),
            "visio": self.save_visio()
        }

        with open(self.RESULT_LOCATION, 'w') as f:
            f.write(json.dumps(result))


class LocalGame(Game):
    def __init__(self, clients, scene, timeout):
        super().__init__(clients)
        self.scene = scene
        self.timeout = timeout

    def append_bonuses_to_leaderboard(self):
        for player in self.players:
            if len(player.bonuses) > 0:
                for bonus in player.bonuses:
                    label = '{} - {} - {}'.format(player.name, bonus.name, bonus.get_remaining_ticks())
                    self.scene.append_label_to_leaderboard(label, player.color)

    def append_losers_to_leaderboard(self):
        for player in self.losers:
            label = '{} выбыл, результат: {}'.format(player.name, player.score)
            self.scene.append_label_to_leaderboard(label, player.color)

    def append_scores_to_leaderboard(self):
        for player in self.players:
            label = '{} результат: {}'.format(player.name, player.score)
            self.scene.append_label_to_leaderboard(label, player.color)

    def draw_bonuses(self):
        for bonus in self.bonuses:
            bonus.draw()

    def draw_leaderboard(self):
        self.append_losers_to_leaderboard()
        self.append_scores_to_leaderboard()
        self.append_bonuses_to_leaderboard()
        self.scene.draw_leaderboard()

    def draw(self):
        for player in self.players:
            player.territory.draw()

        Saw.draw_lines()
        Saw.draw_territories()

        for player in self.players:
            player.draw_lines()

        for player in self.players:
            player.draw_position()

        self.scene.draw_border()
        self.draw_bonuses()
        # self.scene.draw_grid()
        self.draw_leaderboard()

        if len(self.players) == 0:
            self.scene.show_game_over()
        elif self.timeout and self.tick >= MAX_TICK_COUNT:
            self.scene.show_game_over(timeout=True)

    async def game_loop(self, *args, **kwargs):
        self.scene.clear()
        self.draw()
        return await super().game_loop(*args, **kwargs)


class RewindGame(Game):
    def __init__(self, clients, _, timeout, host=None, port=None):
        super().__init__(clients)
        self.client = RewindClient(host, port)
        self.timeout = timeout

        self.to_draw = []

    def print_bonuses(self):
        for player in self.players:
            if len(player.bonuses) > 0:
                for bonus in player.bonuses:
                    label = '{} - {} - {}'.format(player.name, bonus.name, bonus.get_remaining_ticks())
                    self.to_draw.append(self.client.message(label))

    def print_losers(self):
        for player in self.losers:
            label = '{} lost, score: {}'.format(player.name, player.score)
            self.to_draw.append(self.client.message(label))

    def print_scores(self):
        for player in self.players:
            label = '{} score: {}'.format(player.name, player.score)
            self.to_draw.append(self.client.message(label))

    def draw_bonuses(self):
        for bonus in self.bonuses:
            self.to_draw.append(self.client.cell(bonus.x, bonus.y, bonus.color))
            self.to_draw.append(self.client.cell_popup(bonus.x, bonus.y, bonus.name))

    def print_leaderboard(self):
        self.print_losers()
        self.print_scores()
        self.print_bonuses()

    def draw_cell_popup(self):
        width = WIDTH // 2
        for xi, x in enumerate(range(width, WINDOW_WIDTH, WIDTH)):
            for yi, y in enumerate(range(width, WINDOW_HEIGHT, WIDTH)):
                self.to_draw.append(self.client.cell_popup(x, y, f'{xi}:{yi}'))

    def draw_border(self):
        self.to_draw.append(self.client.line(0, 0, 0, WINDOW_HEIGHT, Scene.border_color))
        self.to_draw.append(self.client.line(0, WINDOW_HEIGHT, WINDOW_WIDTH, WINDOW_HEIGHT, Scene.border_color))
        self.to_draw.append(self.client.line(WINDOW_WIDTH, WINDOW_HEIGHT, WINDOW_WIDTH, 0, Scene.border_color))
        self.to_draw.append(self.client.line(WINDOW_WIDTH, 0, 0, 0, Scene.border_color))

    def draw(self):
        for player in self.players:
            for x, y in player.territory.points:
                self.to_draw.append(self.client.cell(x, y, player.territory.color))
                self.to_draw.append(self.client.cell_popup(x, y, f'{player.name}`s territory'))

        for pair in Saw.lines[:]:
            pair[0] -= Saw.opacity_step
            if pair[0] <= 0:
                Saw.lines.remove(pair)
            else:
                for x, y in pair[1]:
                    self.to_draw.append(self.client.cell(x, y, (*Saw.line_color, pair[0])))

        for pair in Saw.territories[:]:
            pair[0] = [*pair[0][:3], pair[0][3] - Saw.opacity_step]
            if pair[0][3] <= 0:
                Saw.territories.remove(pair)
            else:
                for x, y in pair[1]:
                    self.to_draw.append(self.client.cell(x, y, pair[0]))

        for player in self.players:
            for x, y in player.lines:
                self.to_draw.append(self.client.cell(x, y, player.line_color))
                self.to_draw.append(self.client.cell_popup(x, y, f'{player.name}`s line'))

        for number, player in enumerate(self.players):
            self.to_draw.append(self.client.cell(player.x, player.y, player.color))
            self.to_draw.append(self.client.cell_popup(player.x, player.y, player.name))

            if player.rewind:
                for obj in player.rewind:
                    obj.update({'layer': number + 2})
                    self.to_draw.append(obj)

        self.draw_cell_popup()
        self.draw_border()
        self.draw_bonuses()
        self.print_leaderboard()

        if len(self.players) == 0:
            self.to_draw.append(self.client.message('GAME OVER'))
        elif self.timeout and self.tick >= MAX_TICK_COUNT:
            self.to_draw.append(self.client.message('TIMEOUT'))

        self.client.end_frame(self.to_draw)

    def game_save(self):
        if SAVE:
            super().game_save()

    async def game_loop(self, *args, **kwargs):
        self.to_draw.clear()
        self.draw()
        return await super().game_loop(*args, **kwargs)
