// Included by the generated standalone crate in duel_versions.py.
use engine::Game;
use std::time::Instant;
enum AI {
    Reference(reference::Pool),
    Candidate(candidate::Pool),
}
impl AI {
    fn new(name: &str, capacity: usize, seed: u64) -> Self {
        match name {
            "reference" => Self::Reference(reference::Pool::with_seed(capacity, seed)),
            "candidate" => Self::Candidate(candidate::Pool::with_seed(capacity, seed)),
            _ => panic!("unknown player"),
        }
    }
    fn advance(&mut self, mv: (u8, u8)) {
        macro_rules! step {
            ($p:expr) => {
                if $p.find_child(mv).is_none() {
                    $p.recycle();
                }
            };
        }
        match self {
            Self::Reference(p) => step!(p),
            Self::Candidate(p) => step!(p),
        }
    }
    fn search(&mut self, g: &Game, iters: u64, budget: f64) -> ((u8, u8), [i64; 3], u64) {
        let cells: Vec<_> = g.cells.iter().flatten().copied().collect();
        macro_rules! go {
            ($p:expr,$module:ident) => {{
                let pos = $module::Pos::from_flat(&cells, &g.grids, g.forced, g.turn);
                let mv = $p
                    .search(&pos, 1, iters, budget)
                    .expect("nonterminal search must return a move");
                (mv, $p.stats, $p.done)
            }};
        }
        match self {
            Self::Reference(p) => go!(p, reference),
            Self::Candidate(p) => go!(p, candidate),
        }
    }
}
fn rand(rng: &mut u64) -> u64 {
    *rng ^= *rng << 13;
    *rng ^= *rng >> 7;
    *rng ^= *rng << 17;
    *rng
}
fn opening(seed: u64, plies: usize) -> Game {
    let mut rng = seed | 1;
    let mut game = Game::new();
    for _ in 0..plies {
        let legal = game.legal_moves();
        let mv = legal[rand(&mut rng) as usize % legal.len()];
        assert!(game.apply_move(mv.0 as usize, mv.1 as usize));
        assert_eq!(game.winner, 0, "opening ended");
    }
    game
}
fn main() {
    let args: Vec<_> = std::env::args().collect();
    assert!(
        args.len() == 8,
        "mode games iterations milliseconds seed candidate_capacity reference_capacity"
    );
    let mode = &args[1];
    let left = "candidate";
    let right = "reference";
    let games: usize = args[2].parse().unwrap();
    let iters: u64 = args[3].parse().unwrap();
    let budget = args[4].parse::<f64>().unwrap() / 1000.;
    let seed: u64 = args[5].parse().unwrap();
    let a_cap = args[6].parse().unwrap();
    let b_cap = args[7].parse().unwrap();
    let mut totals = [0; 3];
    let mut differences = 0;
    let mut positions = 0;
    for round in 0..games {
        let pair = round / 2;
        let game_seed = seed.wrapping_add((pair as u64 + 1) * 0x9e3779b9);
        let mut game = opening(game_seed, [0, 8, 16, 24][pair % 4]);
        let a_color = (round % 2 + 1) as u8;
        let mut a = AI::new(left, a_cap, game_seed);
        let mut b = AI::new(right, b_cap, game_seed);
        let mut moves = Vec::new();
        let mut elapsed = [0.; 2];
        let mut done = [0u64; 2];
        let mut plies = 0;
        while game.winner == 0 {
            let index = usize::from(game.turn != a_color);
            let mv = if mode == "equiv" {
                let x = a.search(&game, iters, budget);
                let y = b.search(&game, iters, budget);
                positions += 1;
                if x != y {
                    differences += 1;
                }
                x.0
            } else {
                let now = Instant::now();
                let result = if index == 0 {
                    a.search(&game, iters, budget)
                } else {
                    b.search(&game, iters, budget)
                };
                elapsed[index] += now.elapsed().as_secs_f64();
                done[index] += result.2;
                result.0
            };
            assert!(
                game.apply_move(mv.0 as usize, mv.1 as usize),
                "illegal AI move"
            );
            moves.push([mv.0, mv.1]);
            a.advance(mv);
            b.advance(mv);
            plies += 1;
            assert!(plies <= 81);
        }
        let outcome = if game.winner == 3 {
            1
        } else if game.winner == a_color {
            0
        } else {
            2
        };
        totals[outcome] += 1;
        println!("{{\"game\":{},\"pair\":{},\"seed\":{},\"opening_plies\":{},\"moves\":{:?},\"left\":\"{}\",\"right\":\"{}\",\"left_color\":{},\"outcome\":{},\"plies\":{},\"seconds\":{:?},\"iterations\":{:?},\"wdl\":{:?},\"positions\":{},\"differences\":{}}}",round,pair,game_seed,[0,8,16,24][pair%4],moves,left,right,a_color,outcome,plies,elapsed,done,totals,positions,differences);
    }
}
